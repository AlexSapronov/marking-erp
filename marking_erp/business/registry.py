"""Connect business allocations to the common immutable code/file history."""
from sqlalchemy import text
from fastapi import HTTPException
from ..registry import capture_code,save_payload_file,save_label
from ..file_store import FileStore


def capture(current,row,code,settings,*,quantity_change=False):
    c=current.connection
    result=capture_code(c,row['organization_id'],row['owner_id'],bytes(code['payload']),
        actor_id=current.id,source_ref='business:'+str(row['id'])+':'+str(row['revision']),
        allow_quantity_change=quantity_change)
    if result['conflict']:
        raise HTTPException(409,'Полный КМ конфликтует с реестром. Требуется сверка исходных данных.')
    save_payload_file(c,FileStore(settings.file_storage_path),result['payload_id'])
    c.execute(text('''UPDATE business_codes SET registry_code_id=:code,registry_payload_id=:payload
        WHERE id=:id AND organization_id=:org'''),{'id':code['id'],'org':row['organization_id'],'code':result['id'],'payload':result['payload_id']})
    # Reuse changes allocation ownership, while past events retain their actors.
    c.execute(text('UPDATE code_access SET owner_id=:owner WHERE code_id=:code AND organization_id=:org'),
        {'code':result['id'],'org':row['organization_id'],'owner':row['owner_id']})
    return result


def label(current,row,code,settings,data):
    if code['registry_payload_id'] is None:
        result=capture(current,row,code,settings)
        payload_id=result['payload_id']
    else:payload_id=code['registry_payload_id']
    c=current.connection
    existing=c.execute(text("SELECT id FROM files WHERE payload_id=:payload AND kind='label_pdf' AND snapshot->>'pn'=:pn LIMIT 1"),
        {'payload':payload_id,'pn':code['pn']}).scalar()
    if existing:return existing
    return save_label(c,FileStore(settings.file_storage_path),payload_id,code['pn'],rendered=data)


def backfill(c,settings,actor_id):
    """Maintenance-only, bounded by organization access; no network or rendering."""
    from io import BytesIO
    from hashlib import sha256
    import zipfile
    from .. import auth
    from . import store
    user=c.execute(text("SELECT * FROM users WHERE id=:id AND enabled AND group_slug='administrator'"),{'id':actor_id}).mappings().first()
    if not user:raise ValueError('Нужен действующий администратор')
    current=auth.Actor(c,dict(user),'maintenance',None)
    c.execute(text('SELECT pg_advisory_xact_lock(683273529)'))
    codes=c.execute(text('''SELECT bc.* FROM business_codes bc JOIN user_organizations u ON u.organization_id=bc.organization_id AND u.user_id=:actor
        WHERE bc.registry_payload_id IS NULL AND bc.item_id IS NOT NULL ORDER BY bc.item_id,bc.id FOR UPDATE OF bc'''),{'actor':actor_id}).mappings().all()
    count=0
    for code in codes:
        row=store.item(c,code['item_id'],lock=True)
        result=capture(current,row,code,settings)
        # Copy the exact saved PDF, never produce a new label during maintenance.
        saved=c.execute(text("SELECT body,snapshot FROM business_artifacts WHERE item_id=:id AND kind='labels' ORDER BY version DESC LIMIT 1"),{'id':row['id']}).mappings().first()
        if saved:
            expected=sha256(bytes(code['payload'])).hexdigest()
            index=next((i for i,v in enumerate(saved['snapshot']['codes']) if v['cis']==code['cis'] and v['pn']==code['pn'] and v['quantity']==code['quantity'] and v['sha256']==expected),None)
            if index is not None:
                with zipfile.ZipFile(BytesIO(bytes(saved['body']))) as archive:data=archive.read(f'{index+1:05d}.pdf')
                label(current,row,{**code,'registry_payload_id':result['payload_id']},settings,data)
        count+=1
    auth.audit(c,actor_id,'registry.business_backfilled',None,{'count':count})
    return count

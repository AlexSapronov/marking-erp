"""Organization and own/assigned filtering happen before any payload or file read."""
from uuid import UUID
from fastapi import Depends,HTTPException,Query,Response
from sqlalchemy import text
from . import auth
from .file_store import FileStore,StorageError


def assignment_sql(code,org):
    return f'''(EXISTS(SELECT 1 FROM code_assignments x WHERE x.code_id={code} AND x.organization_id={org} AND x.user_id=:user)
        OR EXISTS(SELECT 1 FROM business_codes bc JOIN business_items bi ON bi.id=bc.item_id
            JOIN warehouse_tasks w ON w.source_id=bi.source_id AND w.organization_id=bi.organization_id
            JOIN operation_assignments wa ON wa.target_id=w.id AND wa.user_id=:user
            WHERE bc.registry_code_id={code} AND bc.organization_id={org} AND w.state<>'cancelled'))'''


def install_registry(app,actor,settings):
    dependency=Depends(actor,scope='function')

    def list_scope(current,org,permission):
        grant=current.grants().get(permission)
        if not grant:raise HTTPException(403,'Реестр недоступен вашей группе')
        if org not in current.organizations():raise HTTPException(403,'Нет доступа к организации')
        return grant

    def allowed_code(current,id,org,permission):
        access=current.connection.execute(text(f'''SELECT a.*,{assignment_sql('a.code_id','a.organization_id')} AS assigned
            FROM code_access a WHERE a.code_id=:id AND a.organization_id=:org'''),{'user':current.id,'id':id,'org':org}).mappings().first()
        if not access:raise HTTPException(404,'Код не найден')
        current.require(permission,organization_id=org,owner_id=access['owner_id'],assigned=access['assigned'])
        return access

    @app.get('/api/codes')
    def codes(organization_id:UUID,q:str=Query('',max_length=120),before_id:UUID|None=None,limit:int=Query(25,ge=1,le=100),current=dependency):
        scope=list_scope(current,organization_id,'codes.registry.view')
        result=current.connection.execute(text(f'''SELECT c.id,c.cis,c.gtin,c.created_at,
            (SELECT count(*) FROM code_events e WHERE e.code_id=c.id AND e.organization_id=:org AND e.kind='payload_conflict') AS conflicts,
            v.template,v.label_quantity,v.created_at AS captured_at,v.version,
            COALESCE((SELECT bc.pn FROM business_codes bc WHERE bc.registry_code_id=c.id AND bc.organization_id=:org),(SELECT string_agg(DISTINCT p.pn,', ' ORDER BY p.pn) FROM product_pns p JOIN products pr ON pr.id=p.product_id WHERE pr.gtin=c.gtin AND p.organization_id=:org)) AS pn
            FROM codes c JOIN code_access a ON a.code_id=c.id AND a.organization_id=:org
            JOIN LATERAL (SELECT template,label_quantity,created_at,version FROM code_payload_versions p WHERE code_id=c.id AND organization_id=:org AND EXISTS(SELECT 1 FROM code_events e WHERE e.payload_id=p.id AND e.kind<>'payload_conflict') ORDER BY version DESC LIMIT 1) v ON true
            WHERE (:scope='all' OR (:scope='own' AND a.owner_id=:user) OR (:scope='assigned' AND {assignment_sql('c.id',':org')}))
            AND (CAST(:before AS uuid) IS NULL OR (c.created_at,c.id)<(SELECT created_at,id FROM codes WHERE id=CAST(:before AS uuid)))
            AND (c.cis LIKE :q ESCAPE '!' OR c.gtin LIKE :q ESCAPE '!' OR EXISTS(SELECT 1 FROM product_pns p JOIN products pr ON pr.id=p.product_id WHERE pr.gtin=c.gtin AND p.organization_id=:org AND p.pn ILIKE :q ESCAPE '!'))
            ORDER BY c.created_at DESC,c.id DESC LIMIT :limit'''),{'org':organization_id,'scope':scope,'user':current.id,'before':str(before_id) if before_id else None,'q':'%'+q.replace('!','!!').replace('%','!%').replace('_','!_')+'%','limit':limit+1}).mappings().all()
        return {'codes':[dict(x) for x in result[:limit]],'next_cursor':str(result[limit-1]['id']) if len(result)>limit else None}

    @app.get('/api/codes/{id}/history')
    def history(id:UUID,organization_id:UUID,before_id:int|None=None,current=dependency):
        allowed_code(current,id,organization_id,'codes.history.view')
        rows=current.connection.execute(text('''SELECT e.id,e.kind,e.created_at,u.display_name AS actor_name,v.id AS payload_id,v.version,v.profile,v.corrections,v.raw_sha256,v.normalized_sha256,v.template,v.label_quantity,v.source_ref
            FROM code_events e JOIN code_payload_versions v ON v.id=e.payload_id JOIN users u ON u.id=e.actor_id
            WHERE e.code_id=:id AND e.organization_id=:org AND (CAST(:before AS bigint) IS NULL OR e.id<CAST(:before AS bigint)) ORDER BY e.id DESC LIMIT 26'''),{'id':id,'org':organization_id,'before':before_id}).mappings().all()
        files=current.connection.execute(text('''SELECT id,kind,payload_id,sha256,size,snapshot,created_at FROM files WHERE code_id=:id AND organization_id=:org ORDER BY created_at DESC'''),{'id':id,'org':organization_id}).mappings().all()
        return {'history':[dict(r) for r in rows[:25]],'next_cursor':rows[24]['id'] if len(rows)>25 else None,'files':[dict(f) for f in files]}

    @app.get('/api/codes/{id}/payload/{payload_id}')
    def payload(id:UUID,payload_id:UUID,organization_id:UUID,current=dependency):
        allowed_code(current,id,organization_id,'codes.payload.export')
        row=current.connection.execute(text('SELECT raw,raw_sha256 FROM code_payload_versions WHERE id=:payload AND code_id=:id AND organization_id=:org'),{'payload':payload_id,'id':id,'org':organization_id}).mappings().first()
        if not row:raise HTTPException(404,'Версия КМ не найдена')
        from hashlib import sha256
        data=bytes(row['raw'])
        if sha256(data).hexdigest()!=row['raw_sha256']:raise HTTPException(503,'Нарушена целостность данных')
        auth.audit(current.connection,current.id,'code.payload.downloaded',payload_id,{'code_id':str(id),'organization_id':str(organization_id)})
        return Response(data,media_type='application/octet-stream',headers={'Content-Disposition':f'attachment; filename="code-{payload_id}.bin"'})

    @app.get('/api/files/{id}')
    def download(id:UUID,current=dependency):
        row=current.connection.execute(text('SELECT * FROM files WHERE id=:id'),{'id':id}).mappings().first()
        if not row:raise HTTPException(404,'Файл не найден')
        permission='codes.payload.export' if row['kind']=='payload' else 'marking.labels.download'
        allowed_code(current,row['code_id'],row['organization_id'],permission)
        if row['kind']=='label_pdf':
            from .incidents import enforce
            cis=current.connection.execute(text('SELECT cis FROM codes WHERE id=:id'),{'id':row['code_id']}).scalar_one()
            enforce(current.connection,row['organization_id'],'reprint',cises=[cis])
            allocation=current.connection.execute(text('SELECT state,registry_payload_id FROM business_codes WHERE registry_code_id=:code AND organization_id=:org'),{'code':row['code_id'],'org':row['organization_id']}).mappings().first()
            if allocation and (allocation['state'] in ('free','withdrawn') or allocation['registry_payload_id']!=row['payload_id']):
                raise HTTPException(409,'Состав этикетки изменён или КМ возвращён в пул. Используйте последнюю сохранённую этикетку.')
        try:data=FileStore(settings.file_storage_path).read(row)
        except StorageError:raise HTTPException(503,'Файл недоступен или повреждён') from None
        auth.audit(current.connection,current.id,'file.downloaded',id,{'organization_id':str(row['organization_id']),'sha256':row['sha256']})
        extension='pdf' if row['kind']=='label_pdf' else 'bin'
        return Response(data,media_type=row['media_type'],headers={'Content-Disposition':f'attachment; filename="artifact-{id}.{extension}"'})

    @app.get('/api/products')
    def products(organization_id:UUID,q:str=Query('',max_length=120),current=dependency):
        current.require('nk.cards.view',organization_id=organization_id)
        rows=current.connection.execute(text('''SELECT pr.id,pr.gtin,p.pn,p.tnved,p.source,p.source_ref,p.evidence_sha256 FROM product_pns p JOIN products pr ON pr.id=p.product_id
            WHERE p.organization_id=:org AND (pr.gtin LIKE :q ESCAPE '!' OR p.pn ILIKE :q ESCAPE '!') ORDER BY pr.gtin,p.pn,p.created_at LIMIT 100'''),{'org':organization_id,'q':'%'+q.replace('!','!!').replace('%','!%').replace('_','!_')+'%'}).mappings().all()
        return {'products':[dict(r) for r in rows]}

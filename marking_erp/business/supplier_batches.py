"""Supplier batches include every pending invoice, independently of list pagination."""
from uuid import UUID
from fastapi import Depends,HTTPException,Query
from sqlalchemy import text
from . import store as s
from .suppliers import expression,canonical

PENDING='''FROM bm_documents d LEFT JOIN business_items m ON m.source_id=d.id AND m.kind='marking' AND m.state<>'archived'
    WHERE d.organization_id=:org AND d.state<>'archived'
    AND EXISTS(SELECT 1 FROM jsonb_array_elements(d.lines) l WHERE COALESCE((l->>'marked')::boolean,true))
    AND NOT EXISTS(SELECT 1 FROM business_items a WHERE a.organization_id=d.organization_id AND a.kind='atk' AND a.state<>'archived'
        AND a.body->'documents' @> jsonb_build_array(CAST(m.id AS text)))'''


def pending(c,org,supplier):
    return c.execute(text(f'''SELECT d.id,d.owner_id,d.number,d.state AS source_state,d.revision,m.id AS marking_id,m.state,m.body '''+PENDING+f''' AND {expression('d.supplier','d.organization_id')}=:supplier ORDER BY d.created_at,d.id'''),{'org':org,'supplier':canonical(c,org,supplier)}).mappings().all()


def require_ready(c,org,supplier,ids):
    values=pending(c,org,supplier)
    if not values or any(v['state']!='applied' or v['source_state']!='prepared' or v['body']['source_revision']!=v['revision'] for v in values):
        raise HTTPException(409,'Дождитесь успешных отчётов по всем накладным поставщика. Ненужные накладные явно перенесите в архив.')
    if {v['marking_id'] for v in values}!=set(ids):raise HTTPException(409,'Включите все ожидающие АТК накладные поставщика. Обновите список партии.')
    return values


def install_supplier_batches(app,actor):
    dep=Depends(actor,scope='function')

    @app.get('/api/atks/supplier-batches')
    def suppliers(organization_id:UUID,after:str=Query('',max_length=200),q:str=Query('',max_length=120),current=dep):
        grant=current.grants().get('marking.atk.create')
        if not grant or organization_id not in current.organizations():raise HTTPException(403,'Раздел недоступен')
        values=current.connection.execute(text(f'''SELECT DISTINCT {expression('d.supplier','d.organization_id')} AS supplier '''+PENDING+f'''
            AND (:grant='all' OR (:grant='own' AND d.owner_id=:user) OR (:grant='assigned' AND EXISTS(SELECT 1 FROM operation_assignments a WHERE a.target_id=d.id AND a.user_id=:user)))
            AND {expression('d.supplier','d.organization_id')}>:after AND {expression('d.supplier','d.organization_id')} ILIKE :q ESCAPE '!' ORDER BY supplier LIMIT 101'''),
            {'org':organization_id,'grant':grant,'user':current.id,'after':after,'q':'%'+q.replace('!','!!').replace('%','!%').replace('_','!_')+'%'}).scalars().all()
        return {'suppliers':values[:100],'next_cursor':values[99] if len(values)>100 else None}

    @app.get('/api/atks/supplier-batch')
    def detail(organization_id:UUID,supplier:str=Query(min_length=1,max_length=200),current=dep):
        if not current.grants().get('marking.atk.create') or organization_id not in current.organizations():raise HTTPException(403,'Раздел недоступен')
        values=pending(current.connection,organization_id,supplier);visible=[];blocked=False;restricted=False
        for row in values:
            assigned=current.connection.execute(text('SELECT EXISTS(SELECT 1 FROM operation_assignments WHERE target_id=:id AND user_id=:user)'),{'id':row['marking_id'] or row['id'],'user':current.id}).scalar_one()
            try:current.require('marking.atk.create',organization_id=organization_id,owner_id=row['owner_id'],assigned=assigned)
            except HTTPException:
                restricted=True;continue
            ready=row['state']=='applied' and row['source_state']=='prepared' and row['body']['source_revision']==row['revision']
            blocked=blocked or not ready
            visible.append({'id':row['marking_id'],'number':row['number'],'ready':ready,'state':row['state'] or row['source_state']})
        return {'supplier':supplier,'documents':visible,'ready':bool(visible) and not blocked and not restricted and len(visible)<=1000,'restricted':restricted,'too_large':len(visible)>1000}

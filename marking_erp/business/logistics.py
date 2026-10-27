"""Logistics completion derived from confirmed upstream facts, never hand-set success."""
from datetime import datetime,timezone
from uuid import UUID
from fastapi import Depends,HTTPException,Query
from pydantic import Field,StrictInt
from sqlalchemy import text
from ..auth_routes import Body
from . import store as s
from .marking import validate_source


class Complete(Body):
    revision:StrictInt=Field(ge=1)
    confirmation:str=Field(min_length=1,max_length=120)
    reason:str=Field(default='',max_length=1000)


def facts(c,row):
    """Return local evidence blockers. Reading this does not call an external API."""
    source=c.execute(text('SELECT * FROM bm_documents WHERE id=:id'),{'id':row['source_id']}).mappings().one()
    blockers=[]
    if row['body'].get('warehouse_composition',{}).get('state')!='ready':blockers.append('Нужен полный Excel поставки: верните накладную в БМ для дополнения немаркируемых строк')
    if source['state']=='archived' or source['revision']!=row['body']['source_revision']:blockers.append('Исходная накладная остановлена или изменена')
    marked=any(r.get('marked',True) for r in row['body']['lines'])
    if (marked and row['state'] not in ('applied','awaiting_full_invoice','warehouse_ready','completed')) or (not marked and row['state'] not in ('unmarked_ready','completed')):blockers.append('Нет подтверждённого отчёта о нанесении')
    codes=c.execute(text('SELECT id,state FROM business_codes WHERE item_id=:id'),{'id':row['id']}).mappings().all()
    if len(codes)!=sum(v['labels'] for v in row['body']['lines']) or any(v['state']!='applied' for v in codes):blockers.append('Не все КМ подтверждены как нанесённые')
    wanted={str(v['id']) for v in codes}
    atks=c.execute(text("SELECT id,state,revision,body FROM business_items WHERE organization_id=:org AND kind='atk' AND state<>'archived' AND body->'documents' @> CAST(:doc AS jsonb)"),{'org':row['organization_id'],'doc':'["'+str(row['id'])+'"]'}).mappings().all()
    covered=set()
    for atk in atks:
        if atk['state']=='transferred' and atk['body'].get('atk'):covered.update(atk['body']['codes'])
    if marked and (not wanted or not wanted<=covered):blockers.append('Не все КМ входят в готовые АТК, переданные поставщику')
    # Every current marking order uses releaseMethodType=IMPORT. Manual dossier
    # preparation is insufficient; require real document and INTRODUCED readback.
    fts=c.execute(text("SELECT id,body FROM business_items WHERE organization_id=:org AND kind='fts' AND state='circulation_confirmed'"),{'org':row['organization_id']}).mappings().all()
    introduced={v['id'] for item in fts if item['body'].get('external_document_id') for v in item['body']['codes']}
    if marked and (not wanted or not wanted<=introduced):blockers.append('Не подтверждён ввод всех КМ в оборот по документу ФТС')
    tasks=c.execute(text("SELECT id,state,source_revision,verified_at,kind FROM warehouse_tasks WHERE source_id=:id AND state<>'cancelled'"),{'id':source['id']}).mappings().all()
    required=source['policy_snapshot']['values'].get('logistics.receipt.required',True)
    receipts=[t for t in tasks if t['kind']=='receipt']
    if required and not receipts:blockers.append('Накладная не передана в обязательную приёмку')
    for task in tasks:
        if task['state']!='closed' or task['source_revision']!=source['revision']:blockers.append('Есть незавершённое или устаревшее складское задание');break
        if marked and task['verified_at'] is None:blockers.append('Склад выполнил физическую сверку без подтверждения ЧЗ');break
    if c.execute(text("SELECT EXISTS(SELECT 1 FROM business_items WHERE kind='transform' AND body->>'atk_item'=ANY(CAST(:ids AS text[])) AND state NOT IN ('done','archived','rejected'))"),{'ids':[str(a['id']) for a in atks]}).scalar_one():blockers.append('Есть незавершённая трансформация АТК')
    if c.execute(text("SELECT EXISTS(SELECT 1 FROM external_operations o JOIN business_links l ON l.operation_id=o.id WHERE l.item_id=:id AND (o.state NOT IN ('confirmed','rejected','cancelled_before_dispatch') OR (o.state='confirmed' AND NOT l.applied)))"),{'id':row['id']}).scalar_one():blockers.append('Есть неразобранная внешняя операция')
    return {'blockers':blockers,'can_complete':not blockers,'composition_request_available':bool(wanted) and wanted<=covered and row['state'] in ('applied','warehouse_ready') and not tasks,'receipt_required':required,
        'atks':[{'id':a['id'],'revision':a['revision'],'state':a['state']} for a in atks],
        'fts':[f['id'] for f in fts if any(v['id'] in wanted for v in f['body']['codes'])],
        'tasks':[dict(t) for t in tasks]}


def is_completed(c,source_id):
    return c.execute(text("SELECT EXISTS(SELECT 1 FROM business_items WHERE kind='marking' AND source_id=:id AND state='completed')"),{'id':source_id}).scalar_one()


def has_completed_dependents(c,row):
    if row['kind']=='atk':documents=row['body']['documents']
    elif row['kind']=='fts':
        documents=c.execute(text("SELECT DISTINCT jsonb_array_elements_text(body->'documents') FROM business_items WHERE kind='atk' AND organization_id=:org AND id=ANY(CAST(:ids AS uuid[]))"),{'org':row['organization_id'],'ids':row['body']['atks']}).scalars().all()
    else:return False
    return c.execute(text("SELECT EXISTS(SELECT 1 FROM business_items WHERE id=ANY(CAST(:ids AS uuid[])) AND organization_id=:org AND kind='marking' AND state='completed')"),{'ids':documents,'org':row['organization_id']}).scalar_one()


def install_logistics(app,actor):
    dep=Depends(actor,scope='function')

    @app.get('/api/logistics/documents')
    def listing(organization_id:UUID,completed:bool=False,before_id:UUID|None=None,q:str=Query('',max_length=120),current=dep):
        grant=current.grants().get('logistics.documents.complete')
        if not grant or organization_id not in current.organizations():raise HTTPException(403,'Раздел недоступен')
        values=current.connection.execute(text('''SELECT i.* FROM business_items i WHERE i.organization_id=:org AND i.kind='marking' AND i.state<>'archived'
            AND (i.state='completed')=:completed
            AND (:grant='all' OR (:grant='own' AND i.owner_id=:user) OR (:grant='assigned' AND EXISTS(SELECT 1 FROM operation_assignments a WHERE a.target_id=i.id AND a.user_id=:user)))
            AND (CAST(:before AS uuid) IS NULL OR (i.created_at,i.id)<(SELECT created_at,id FROM business_items WHERE id=CAST(:before AS uuid) AND organization_id=:org))
            AND (i.title ILIKE :q ESCAPE '!' OR i.body->>'supplier' ILIKE :q ESCAPE '!') ORDER BY i.created_at DESC,i.id DESC LIMIT 26'''),
            {'org':organization_id,'completed':completed,'grant':grant,'user':current.id,'before':str(before_id) if before_id else None,'q':'%'+q.replace('!','!!').replace('%','!%').replace('_','!_')+'%'}).mappings().all()
        return {'documents':[{'id':r['id'],'title':r['title'],'supplier':r['body']['supplier'],'state':r['state'],'revision':r['revision']} for r in values[:25]],'next_cursor':str(values[24]['id']) if len(values)>25 else None}

    @app.get('/api/logistics/documents/{id}')
    def detail(id:UUID,current=dep):
        row=s.get(current,id,'marking',permission='logistics.documents.complete')
        return {'document':{'id':row['id'],'title':row['title'],'state':row['state'],'revision':row['revision'],'supplier':row['body']['supplier'],'completion':row['body'].get('logistics_completion'),'composition':row['body'].get('warehouse_composition')},**facts(current.connection,row)}

    @app.post('/api/logistics/documents/{id}/complete')
    def complete(id:UUID,body:Complete,current=dep):
        row=s.get(current,id,'marking',True,'logistics.documents.complete')
        if body.confirmation!=row['title']:raise HTTPException(422,'Введите точный номер накладной')
        if row['state']=='completed':
            if row['body']['logistics_completion']['revision']==body.revision:return {'id':id,'state':'completed','replayed':True}
            raise HTTPException(409,'Накладная уже завершена')
        s.version(row,body.revision);validate_source(current,row)
        proof=facts(current.connection,row)
        if proof['blockers']:raise HTTPException(409,{'message':'Обязательные этапы не завершены','blockers':proof['blockers']})
        data={**row['body'],'logistics_completion':{'revision':body.revision,'actor_id':str(current.id),'completed_at':datetime.now(timezone.utc).isoformat(),'evidence':proof}}
        s.save(current,row,'logistics_completed',body=data,state='completed')
        return {'id':id,'state':'completed'}

    @app.post('/api/logistics/documents/{id}/reopen')
    def reopen(id:UUID,body:Complete,current=dep):
        row=s.get(current,id,'marking',True,'documents.restore')
        if row['state']!='completed' or row['revision']!=body.revision:raise HTTPException(409,'Нужна актуальная завершённая накладная')
        if body.confirmation!=row['title'] or len(body.reason.strip())<8:raise HTTPException(422,'Введите номер и причину от 8 символов')
        s.record(current,row,'logistics_reopened',{'reason':body.reason,'completion':row['body']['logistics_completion']})
        updated=s.save(current,row,'logistics_returned_to_work',state='applied' if any(r.get('marked',True) for r in row['body']['lines']) else 'unmarked_ready')
        return {'id':id,'state':updated['state']}

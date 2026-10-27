"""Permission-gated fallback source, immutable preview and explicit task creation."""
from datetime import date,datetime,timezone,timedelta
from hashlib import sha256
import json
from uuid import UUID,uuid4
from fastapi import Depends,HTTPException,Query,Request,Response
from pydantic import Field
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool
from .auth_routes import Body
from . import auth,warehouse_excel as excel,warehouse_check as checks

MIME='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'


class Commit(Body):
    request_id:UUID
    preview_id:UUID
    number:str=Field(min_length=1,max_length=120)
    document_date:date
    supplier:str=Field(min_length=1,max_length=200)
    reason:str=Field(min_length=8,max_length=1000)
    assignees:list[UUID]=Field(min_length=1,max_length=100)


def eligible(current,org,assignees,kind):
    ids=sorted(set(map(str,assignees)))
    rows=current.connection.execute(text('''SELECT u.id,u.group_slug FROM users u JOIN user_organizations o ON o.user_id=u.id
        WHERE u.enabled AND o.organization_id=:org AND u.id=ANY(CAST(:ids AS uuid[]))
        AND u.group_slug IN ('warehouse','tsd','specialist','administrator')'''),{'org':org,'ids':ids}).mappings().all()
    if {str(p['id']) for p in rows}!=set(ids):raise HTTPException(422,'Проверьте сотрудников и их доступ к организации')
    for p in rows:
        try:auth.Actor(current.connection,dict(p),'',None).require('warehouse.'+kind+'.scan',organization_id=org,owner_id=current.id,assigned=True)
        except HTTPException:raise HTTPException(422,'У выбранного сотрудника нет права сканировать задания этого типа') from None
    return rows


def public(result):
    return {k:v for k,v in result.items() if k not in ('expected_codes','expected_payloads')}


def install(app,actor,require_task,get_task,record,exclusive_codes,task_context):
    dep=Depends(actor,scope='function')

    @app.get('/api/warehouse/imports/template')
    def template(organization_id:UUID,current=dep):
        current.require('warehouse.import.preview',organization_id=organization_id,owner_id=current.id)
        return Response(excel.template(),media_type=MIME,headers={'Content-Disposition':'attachment; filename="warehouse-template.xlsx"'})

    @app.get('/api/warehouse/imports/options')
    def options(organization_id:UUID,kind:str=Query('receipt',pattern='^(receipt|shipment)$'),current=dep):
        current.require('warehouse.import.preview',organization_id=organization_id,owner_id=current.id)
        current.require('warehouse.'+kind+'.view',organization_id=organization_id,owner_id=current.id)
        people=current.connection.execute(text('''SELECT u.id,u.display_name,g.name AS group_name FROM users u JOIN groups g ON g.slug=u.group_slug
            JOIN user_organizations o ON o.user_id=u.id WHERE o.organization_id=:org AND u.enabled
            AND u.group_slug IN ('warehouse','tsd','specialist','administrator') ORDER BY u.display_name'''),{'org':organization_id}).mappings().all()
        return {'assignees':[dict(p) for p in people]}

    @app.post('/api/warehouse/imports/preview')
    async def preview(request:Request,organization_id:UUID,kind:str=Query('receipt',pattern='^(receipt|shipment)$'),filename:str=Query('warehouse.xlsx',min_length=1,max_length=200),current=dep):
        current.require('warehouse.import.preview',organization_id=organization_id,owner_id=current.id)
        current.require('warehouse.'+kind+'.view',organization_id=organization_id,owner_id=current.id)
        parts=[];size=0
        async for part in request.stream():
            size+=len(part)
            if size>10*1024*1024:raise HTTPException(413,'Файл больше 10 МБ')
            parts.append(part)
        raw=b''.join(parts)
        try:result=await run_in_threadpool(excel.read,raw)
        except Exception as error:
            if isinstance(error,ValueError):raise HTTPException(422,str(error)) from None
            raise HTTPException(422,'Не удалось прочитать XLSX. Используйте шаблон.') from None
        response={**public(result),'expected_count':len(result['expected_codes']),'external_calls':False,'preview_id':None}
        if not result['errors']:
            id=uuid4();digest=sha256(raw).hexdigest()
            current.connection.execute(text('''INSERT INTO warehouse_imports(id,organization_id,owner_id,kind,filename,sha256,raw,result)
                VALUES(:id,:org,:owner,:kind,:filename,:sha,:raw,CAST(:result AS jsonb))'''),{'id':id,'org':organization_id,'owner':current.id,'kind':kind,'filename':filename,'sha':digest,'raw':raw,'result':json.dumps(result)})
            auth.audit(current.connection,current.id,'warehouse.import.preview',id,{'kind':kind,'sha256':digest,'codes':len(result['expected_codes'])})
            response.update(preview_id=id,sha256=digest)
        return response

    @app.post('/api/warehouse/imports/commit')
    def commit(body:Commit,current=dep):
        c=current.connection
        if body.request_id==body.preview_id:raise HTTPException(422,'Идентификаторы запроса и предпросмотра должны различаться')
        preview=c.execute(text('SELECT * FROM warehouse_imports WHERE id=:id'),{'id':body.preview_id}).mappings().first()
        if not preview:raise HTTPException(404,'Предпросмотр не найден')
        current.require('warehouse.import.commit',organization_id=preview['organization_id'],owner_id=preview['owner_id'])
        current.require('warehouse.'+preview['kind']+'.view',organization_id=preview['organization_id'],owner_id=current.id)
        fingerprint=checks.digest(body.model_dump(mode='json')|{'assignees':sorted(set(map(str,body.assignees)))})
        existing=c.execute(text('SELECT * FROM warehouse_tasks WHERE id=:id OR (source_id=:source AND source_type=\'excel\')'),{'id':body.request_id,'source':body.preview_id}).mappings().first()
        if existing:
            require_task(current,existing)
            if existing['id']!=body.request_id or existing['source_id']!=body.preview_id or existing['owner_id']!=current.id or existing['transfer_request_sha256']!=fingerprint:
                raise HTTPException(409,'Предпросмотр уже использован или повтор запроса отличается от сохранённого')
            return {'id':existing['id'],'replayed':True}
        if datetime.now(timezone.utc)-preview['created_at']>timedelta(hours=24):raise HTTPException(409,'Предпросмотр старше суток. Загрузите файл заново.')
        if not body.number.strip() or not body.supplier.strip() or len(body.reason.strip())<8:raise HTTPException(422,'Заполните номер, контрагента и причину резервного импорта от 8 символов')
        people=eligible(current,preview['organization_id'],body.assignees,preview['kind'])
        if c.execute(text('SELECT EXISTS(SELECT 1 FROM operation_targets WHERE id=ANY(CAST(:ids AS uuid[])))'),{'ids':[str(body.request_id),str(body.preview_id)]}).scalar_one():raise HTTPException(409,'Идентификатор уже использован')
        result=preview['result'];exclusive_codes(current,{'organization_id':preview['organization_id'],'source_id':body.preview_id},result['expected_codes'])
        for id in (body.preview_id,body.request_id):
            c.execute(text('INSERT INTO operation_targets(id,organization_id,owner_id,revision) VALUES(:id,:org,:owner,1)'),{'id':id,'org':preview['organization_id'],'owner':current.id})
        c.execute(text("INSERT INTO warehouse_sources(id,organization_id,kind,import_id) VALUES(:id,:org,'excel',:id)"),{'id':body.preview_id,'org':preview['organization_id']})
        c.execute(text('''INSERT INTO warehouse_tasks(id,organization_id,owner_id,source_id,source_type,source_revision,kind,number,supplier,document_date,lines,expected_codes,expected_payloads,transfer_request_sha256)
            VALUES(:id,:org,:owner,:source,'excel',1,:kind,:number,:supplier,:date,CAST(:lines AS jsonb),CAST(:codes AS jsonb),CAST(:evidence AS jsonb),:hash)'''),
            {'id':body.request_id,'org':preview['organization_id'],'owner':current.id,'source':body.preview_id,'kind':preview['kind'],'number':body.number.strip(),'supplier':body.supplier.strip(),'date':body.document_date,'lines':json.dumps(result['lines']),'codes':json.dumps(result['expected_codes']),'evidence':json.dumps(result['expected_payloads']),'hash':fingerprint})
        for person in people:c.execute(text('INSERT INTO operation_assignments(target_id,user_id) VALUES(:id,:user)'),{'id':body.request_id,'user':person['id']})
        record(current,get_task(current,body.request_id),'imported',{'reason':body.reason.strip(),'sha256':preview['sha256'],'assignees':sorted(set(map(str,body.assignees)))})
        return {'id':body.request_id}

    @app.get('/api/warehouse/tasks/{id}/source-file')
    def source_file(id:UUID,logistics:bool=False,current=dep):
        task=get_task(current,id);require_task(current,task,logistics=logistics)
        current.require('warehouse.import.preview',**task_context(current,task))
        if task['source_type']!='excel':raise HTTPException(404,'У задания нет резервного Excel')
        preview=current.connection.execute(text('SELECT raw FROM warehouse_imports WHERE id=:id'),{'id':task['source_id']}).scalar_one()
        return Response(bytes(preview),media_type=MIME,headers={'Content-Disposition':'attachment; filename="warehouse-source-'+str(id)+'.xlsx"','Cache-Control':'no-store'})

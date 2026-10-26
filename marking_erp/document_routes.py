"""Local invoice preparation. No issue/report/ATK dispatch or asserted NK provenance."""
from datetime import date
from hashlib import sha256
import json
from uuid import UUID
from fastapi import Depends, HTTPException, Query, Request, Response
from pydantic import Field, StrictInt
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool
from .auth_routes import Body
from . import auth, invoice_excel as excel
from .policy import snapshot
from .operations import cancel_before_dispatch


class Line(Body):
    pn: str=Field(min_length=1,max_length=120)
    gtin: str=Field(default='',max_length=14)
    tnved: str=Field(default='',max_length=10)
    quantity: str|StrictInt
    labels: str|StrictInt


class Invoice(Body):
    organization_id: UUID
    number: str=Field(min_length=1,max_length=120)
    document_date: date
    supplier: str=Field(min_length=1,max_length=200)
    lines: list[Line]=Field(min_length=1,max_length=20000)
    imported: bool=False


class Create(Invoice):
    request_id: UUID


class Edit(Invoice):
    revision: StrictInt=Field(ge=1)


class Change(Body):
    revision: StrictInt=Field(ge=1)
    confirmation: str=Field(default='',max_length=120)
    reason: str=Field(default='',max_length=1000)


def normalize_lines(lines,merge=True):
    result={};pns={};gtins={};warnings=[]
    for n,line in enumerate(lines,1):
        try:
            pn=excel.pn_text(line['pn']);gtin=line['gtin'].strip();tnved=line['tnved'].strip()
            if gtin:
                gtin,padded=excel.identifier(gtin,14,'GTIN')
                if not excel.valid_gtin(gtin):raise ValueError('GTIN: неверная контрольная цифра')
                if padded:warnings.append(f'Строка {n}: GTIN дополнен ведущими нулями')
            if tnved:tnved,_=excel.identifier(tnved,10,'ТН ВЭД')
            quantity=excel.integer_value(line['quantity'],'Штук в этикетке',99999999)
            labels=excel.integer_value(line['labels'],'Этикеток',100000)
            if gtin and pn in pns and pns[pn]!=gtin:raise ValueError('Один PN соответствует разным GTIN')
            if gtin:pns[pn]=gtin
            if gtin and tnved:
                if gtin in gtins and gtins[gtin]!=tnved:raise ValueError('Один GTIN соответствует разным ТН ВЭД')
                gtins[gtin]=tnved
            key=(pn,gtin,tnved,quantity)+(() if merge else (n,))
            if key in result:result[key]['labels']+=labels
            else:result[key]={'pn':pn,'gtin':gtin,'tnved':tnved,'quantity':quantity,'labels':labels}
        except ValueError as error:raise HTTPException(422,f'Строка {n}: {error}') from None
    if sum(row['labels'] for row in result.values())>100000:raise HTTPException(422,'Больше 100000 этикеток в документе')
    if len(result)<len(lines):warnings.append(f'Объединено повторных строк: {len(lines)-len(result)}')
    return list(result.values()),warnings


def install_documents(app,actor):
    dep=Depends(actor,scope='function')

    def require(current,permission,doc):
        assigned=current.connection.execute(text('SELECT EXISTS(SELECT 1 FROM operation_assignments WHERE target_id=:id AND user_id=:user)'),{'id':doc['id'],'user':current.id}).scalar_one()
        current.require(permission,organization_id=doc['organization_id'],owner_id=doc['owner_id'],assigned=assigned)

    def get(current,id,lock=False):
        doc=current.connection.execute(text('SELECT * FROM bm_documents WHERE id=:id'+(' FOR UPDATE' if lock else '')),{'id':id}).mappings().first()
        if not doc:raise HTTPException(404,'Накладная не найдена')
        require(current,'marking.documents.view',doc)
        if doc['state']=='archived':require(current,'documents.archive.view',doc)
        return dict(doc)

    def no_external(c,id):
        if c.execute(text("SELECT EXISTS(SELECT 1 FROM business_items WHERE source_id=:id AND kind='marking' AND state<>'archived')"),{'id':id}).scalar_one():
            raise HTTPException(409,'Накладная уже участвует в маркировке. Сначала остановите процесс.')
        if c.execute(text("SELECT EXISTS(SELECT 1 FROM warehouse_tasks WHERE source_id=:id AND state<>'cancelled')"),{'id':id}).scalar_one():
            raise HTTPException(409,'Накладная передана на склад. Сначала отмените складские задания через логиста.')
        if c.execute(text("SELECT EXISTS(SELECT 1 FROM external_operations WHERE target_id=:id AND state NOT IN ('rejected','cancelled_before_dispatch'))"),{'id':id}).scalar_one():
            raise HTTPException(409,'У документа есть внешние операции. Требуется разбор фактического состояния.')

    def check_tnved(c,org,lines,id):
        c.execute(text("SELECT pg_advisory_xact_lock(hashtextextended('bm-documents:'||CAST(:org AS text),0))"),{'org':org})
        known={r['gtin']:r['tnved'] for r in lines if r['gtin'] and r['tnved']}
        if not known:return
        rows=c.execute(text('''SELECT l->>'gtin' AS gtin,l->>'tnved' AS tnved FROM bm_documents d,
            jsonb_array_elements(d.lines) l WHERE d.organization_id=:org AND d.id<>:id
            AND l->>'gtin'=ANY(CAST(:gtins AS text[])) AND l->>'tnved'<>''
            UNION SELECT p.gtin,n.tnved FROM products p JOIN product_pns n ON n.product_id=p.id
            WHERE n.organization_id=:org AND p.gtin=ANY(CAST(:gtins AS text[]))'''),{'org':org,'id':id,'gtins':list(known)}).mappings()
        if any(known[r['gtin']]!=r['tnved'] for r in rows):raise HTTPException(409,'ТН ВЭД для GTIN расходится с другой накладной или подтверждённым источником')

    def record(current,doc,action,reason=''):
        value={k:doc[k] for k in ('id','organization_id','owner_id','number','document_date','supplier','revision','state','lines','policy_snapshot')}
        current.connection.execute(text('''INSERT INTO bm_document_versions(document_id,organization_id,revision,actor_id,actor_name,action,reason,snapshot)
            VALUES (:id,:org,:revision,:actor,:actor_name,:action,:reason,CAST(:snapshot AS jsonb))'''),{'id':doc['id'],'org':doc['organization_id'],'revision':doc['revision'],'actor':current.id,'actor_name':current.user['display_name'],'action':action,'reason':reason,'snapshot':json.dumps(value,default=str)})
        auth.audit(current.connection,current.id,'document.'+action,doc['id'],{'revision':doc['revision']})

    def save(current,doc,action,reason=''):
        doc['revision']+=1;c=current.connection
        c.execute(text('''UPDATE bm_documents SET number=:number,document_date=:document_date,supplier=:supplier,revision=:revision,
            state=:state,lines=CAST(:lines AS jsonb),policy_snapshot=CAST(:policy AS jsonb),updated_at=now() WHERE id=:id'''),
            {**{k:doc[k] for k in ('id','number','document_date','supplier','revision','state')},'lines':json.dumps(doc['lines']),'policy':json.dumps(doc['policy_snapshot'])})
        c.execute(text('UPDATE operation_targets SET revision=:revision,stopped=:stopped WHERE id=:id'),{'id':doc['id'],'revision':doc['revision'],'stopped':doc['state']=='archived'})
        record(current,doc,action,reason)
        return {'id':doc['id'],'revision':doc['revision'],'state':doc['state']}

    def content(current,body,id,owner=None):
        for value in (body.number,body.supplier):
            if not value.strip() or any(ord(x)<32 for x in value):raise HTTPException(422,'Заполните номер и поставщика без управляющих символов')
        policy=snapshot(current.connection,body.organization_id)
        lines,warnings=normalize_lines([r.model_dump() for r in body.lines],policy['values']['bm.lines.merge']!='never')
        check_tnved(current.connection,body.organization_id,lines,id)
        duplicate=current.connection.execute(text('''SELECT EXISTS(SELECT 1 FROM bm_documents WHERE organization_id=:org AND owner_id=:owner
            AND supplier=:supplier AND number=:number AND document_date=:date AND id<>:id)'''),{'org':body.organization_id,'owner':owner or current.id,'supplier':body.supplier.strip(),'number':body.number.strip(),'date':body.document_date,'id':id}).scalar_one()
        if duplicate:raise HTTPException(409,'У вас уже есть накладная с этим номером, датой и поставщиком')
        return {'number':body.number.strip(),'supplier':body.supplier.strip(),'document_date':body.document_date,'lines':lines,'policy_snapshot':policy},warnings

    @app.get('/api/documents/template')
    def template(organization_id:UUID,current=dep):
        current.require('marking.import.preview',organization_id=organization_id,owner_id=current.id)
        return Response(excel.template(),media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',headers={'Content-Disposition':'attachment; filename="invoice-template.xlsx"'})

    @app.post('/api/documents/import-preview')
    async def preview(request:Request,organization_id:UUID,sheet_name:str|None=Query(None,max_length=100),header_row:int|None=Query(None,ge=0,le=20030),mapping:str|None=Query(None,max_length=500),current=dep):
        current.require('marking.import.preview',organization_id=organization_id,owner_id=current.id)
        try:columns=json.loads(mapping) if mapping else None
        except (ValueError,TypeError):raise HTTPException(422,'Некорректное сопоставление столбцов') from None
        chunks=[];size=0
        async for chunk in request.stream():
            size+=len(chunk)
            if size>10*1024*1024:raise HTTPException(413,'Размер Excel превышает 10 МБ')
            chunks.append(chunk)
        policy=snapshot(current.connection,organization_id)
        result=await run_in_threadpool(excel.import_file,b''.join(chunks),columns,sheet_name,header_row,policy['values']['bm.lines.merge']!='never')
        return {**result,'policy_versions':policy['versions'],'external_calls':False}

    @app.get('/api/documents')
    def listing(organization_id:UUID,archived:bool=False,q:str=Query('',max_length=120),before_id:UUID|None=None,current=dep):
        permission='documents.archive.view' if archived else 'marking.documents.view'
        grant=current.grants().get(permission)
        view=current.grants().get('marking.documents.view')
        if not grant or not view or organization_id not in current.organizations():raise HTTPException(403,'Нет доступа к накладным')
        rows=current.connection.execute(text('''SELECT d.id,d.number,d.document_date,d.supplier,d.owner_id,d.revision,d.state,d.updated_at,
            jsonb_array_length(d.lines) AS line_count,(SELECT sum((l->>'labels')::bigint) FROM jsonb_array_elements(d.lines) l) AS labels
            FROM bm_documents d WHERE d.organization_id=:org AND (d.state='archived')=:archived
            AND (:grant='all' OR (:grant='own' AND d.owner_id=:user) OR (:grant='assigned' AND EXISTS(SELECT 1 FROM operation_assignments a WHERE a.target_id=d.id AND a.user_id=:user)))
            AND (:view='all' OR (:view='own' AND d.owner_id=:user) OR (:view='assigned' AND EXISTS(SELECT 1 FROM operation_assignments a WHERE a.target_id=d.id AND a.user_id=:user)))
            AND (CAST(:before AS uuid) IS NULL OR (d.created_at,d.id)<(SELECT created_at,id FROM bm_documents WHERE id=CAST(:before AS uuid) AND organization_id=:org))
            AND (d.number ILIKE :q ESCAPE '!' OR d.supplier ILIKE :q ESCAPE '!' OR EXISTS(SELECT 1 FROM jsonb_array_elements(d.lines) l WHERE l->>'pn' ILIKE :q ESCAPE '!'))
            ORDER BY d.created_at DESC,d.id DESC LIMIT 26'''),{'org':organization_id,'archived':archived,'grant':grant,'view':view,'user':current.id,'before':str(before_id) if before_id else None,'q':'%'+q.replace('!','!!').replace('%','!%').replace('_','!_')+'%'}).mappings().all()
        return {'documents':[dict(r) for r in rows[:25]],'next_cursor':str(rows[24]['id']) if len(rows)>25 else None,'external_dispatch_available':False}

    @app.post('/api/documents')
    def create(body:Create,current=dep):
        current.require('marking.documents.create',organization_id=body.organization_id,owner_id=current.id)
        if body.imported:current.require('marking.import.commit',organization_id=body.organization_id,owner_id=current.id)
        c=current.connection;hash=sha256(json.dumps(body.model_dump(mode='json'),sort_keys=True).encode()).hexdigest()
        existing=c.execute(text('SELECT * FROM bm_documents WHERE id=:id'),{'id':body.request_id}).mappings().first()
        if existing:
            require(current,'marking.documents.view',existing)
            if existing['state']=='archived':require(current,'documents.archive.view',existing)
            if existing['owner_id']!=current.id or existing['organization_id']!=body.organization_id or existing['request_sha256']!=hash:raise HTTPException(409,'Повторный запрос отличается от сохранённого')
            return {'id':existing['id'],'revision':existing['revision'],'state':existing['state'],'replayed':True}
        if c.execute(text('SELECT EXISTS(SELECT 1 FROM operation_targets WHERE id=:id)'),{'id':body.request_id}).scalar_one():raise HTTPException(409,'Идентификатор уже занят')
        value,warnings=content(current,body,body.request_id)
        c.execute(text('INSERT INTO operation_targets(id,organization_id,owner_id,revision) VALUES (:id,:org,:user,1)'),{'id':body.request_id,'org':body.organization_id,'user':current.id})
        c.execute(text('''INSERT INTO bm_documents(id,organization_id,owner_id,number,document_date,supplier,revision,state,lines,policy_snapshot,request_sha256)
            VALUES (:id,:org,:owner,:number,:date,:supplier,1,'draft',CAST(:lines AS jsonb),CAST(:policy AS jsonb),:hash)'''),
            {'id':body.request_id,'org':body.organization_id,'owner':current.id,'number':value['number'],'date':value['document_date'],'supplier':value['supplier'],'lines':json.dumps(value['lines']),'policy':json.dumps(value['policy_snapshot']),'hash':hash})
        doc=get(current,body.request_id);record(current,doc,'created')
        return {'id':doc['id'],'revision':1,'state':'draft','warnings':warnings}

    @app.get('/api/documents/{id}')
    def detail(id:UUID,current=dep):
        doc=get(current,id)
        return {'document':{k:v for k,v in doc.items() if k!='request_sha256'},'external_dispatch_available':False,
                'notice':'Проверка состава локальная. Для эмиссии и нанесения откройте раздел «Маркировка». Сведения каталога подтверждаются через Национальный каталог.'}

    @app.put('/api/documents/{id}')
    def edit(id:UUID,body:Edit,current=dep):
        doc=get(current,id,True);require(current,'marking.documents.edit',doc)
        if body.organization_id!=doc['organization_id']:raise HTTPException(422,'Организация документа не меняется')
        if body.revision!=doc['revision']:raise HTTPException(409,'Накладная изменена. Обновите данные.')
        if doc['state']=='archived':raise HTTPException(409,'Архивную накладную нельзя изменять')
        no_external(current.connection,id)
        if body.imported:require(current,'marking.import.commit',doc)
        value,warnings=content(current,body,id,doc['owner_id']);doc.update(value,state='draft')
        return {**save(current,doc,'edited'),'warnings':warnings}

    @app.post('/api/documents/{id}/{action}')
    def change(id:UUID,action:str,body:Change,current=dep):
        if action not in ('prepare','archive','restore'):raise HTTPException(404,'Действие не найдено')
        doc=get(current,id,True)
        require(current,{'prepare':'marking.documents.edit','archive':'documents.archive','restore':'documents.restore'}[action],doc)
        if body.revision!=doc['revision']:raise HTTPException(409,'Накладная изменена. Обновите данные.')
        if action=='prepare':
            if doc['state']!='draft':raise HTTPException(409,'Проверять можно только черновик')
            no_external(current.connection,id)
            result=excel.validate_rows([[r[k] for k in ('pn','gtin','quantity','labels','tnved')] for r in doc['lines']],merge=False)
            if result['errors']:raise HTTPException(422,{'message':'Заполните корректные GTIN и ТН ВЭД во всех строках','errors':result['errors'][:100]})
            check_tnved(current.connection,doc['organization_id'],doc['lines'],id);doc['state']='prepared'
        else:
            if body.confirmation!=doc['number'] or len(body.reason.strip())<8:raise HTTPException(422,'Введите точный номер накладной и причину от 8 символов')
            if action=='archive':
                require(current,'documents.stop',doc)
                if doc['state']=='archived':raise HTTPException(409,'Накладная уже в архиве')
                current.connection.execute(text('UPDATE operation_targets SET stopped=true WHERE id=:id'),{'id':id})
                for op in current.connection.execute(text("SELECT id FROM external_operations WHERE target_id=:id AND state IN ('queued','awaiting_signature','blocked') ORDER BY id FOR UPDATE"),{'id':id}).scalars().all():cancel_before_dispatch(current.connection,op)
                for process in current.connection.execute(text("SELECT * FROM business_items WHERE source_id=:id AND kind='marking' FOR UPDATE"),{'id':id}).mappings().all():
                    for operation in current.connection.execute(text('SELECT o.* FROM external_operations o JOIN business_links l ON l.operation_id=o.id WHERE l.item_id=:id ORDER BY o.id FOR UPDATE'),{'id':process['id']}).mappings().all():
                        current.connection.execute(text('UPDATE operation_targets SET stopped=true WHERE id=:id'),{'id':operation['target_id']})
                        if operation['state'] in ('queued','awaiting_signature','blocked'):cancel_before_dispatch(current.connection,operation['id'])
                    current.connection.execute(text("UPDATE business_items SET state='archived',body=jsonb_set(body,'{archived_from}',to_jsonb(state)),revision=revision+1 WHERE id=:id AND state<>'archived'"),{'id':process['id']})
                doc['state']='archived'
            else:
                if doc['state']!='archived':raise HTTPException(409,'Накладная не в архиве')
                no_external(current.connection,id);doc['state']='draft'
        return save(current,doc,{'prepare':'prepared','archive':'archived','restore':'restored'}[action],body.reason.strip())

    @app.get('/api/documents/{id}/history')
    def history(id:UUID,current=dep):
        doc=get(current,id);require(current,'audit.operational.view',doc)
        rows=current.connection.execute(text('''SELECT revision,action,reason,actor_id,actor_name,created_at FROM bm_document_versions
            WHERE document_id=:id ORDER BY revision DESC LIMIT 100'''),{'id':id}).mappings().all()
        return {'history':[dict(r) for r in rows]}

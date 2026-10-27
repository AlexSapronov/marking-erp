"""Logistics returns an emitted invoice to BM to attach its full delivery XLSX."""
from datetime import datetime,timezone
from hashlib import sha256
import json
from uuid import UUID
from fastapi import Depends,HTTPException,Query,Request,Response
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool
from . import store as s
from .marking import validate_source
from .logistics import Complete
from .full_invoice import parse


def install_composition(app,actor):
    dep=Depends(actor,scope='function')

    @app.post('/api/logistics/documents/{id}/request-composition')
    def request_composition(id:UUID,body:Complete,current=dep):
        row=s.get(current,id,'marking',True,'logistics.warehouse.transfer');s.version(row,body.revision);s.idle(current.connection,id);validate_source(current,row)
        if body.confirmation!=row['title']:raise HTTPException(422,'Подтвердите номер накладной')
        if row['state'] not in ('applied','warehouse_ready'):raise HTTPException(409,'Сначала дождитесь подтверждённого отчёта о нанесении')
        c=current.connection
        if c.execute(text("SELECT EXISTS(SELECT 1 FROM warehouse_tasks WHERE source_id=:id AND state<>'cancelled')"),{'id':row['source_id']}).scalar_one():raise HTTPException(409,'Сначала отмените ранее переданные складские задания')
        codes=set(map(str,c.execute(text('SELECT id FROM business_codes WHERE item_id=:id'),{'id':id}).scalars()))
        atks=c.execute(text("SELECT body FROM business_items WHERE organization_id=:org AND kind='atk' AND state='transferred' AND body->'documents' @> CAST(:docs AS jsonb)"),{'org':row['organization_id'],'docs':json.dumps([str(id)])}).scalars().all()
        if not codes or not codes<={v for a in atks for v in a['codes']}:raise HTTPException(409,'Сначала зарегистрируйте АТК и подтвердите их передачу поставщику')
        composition={'state':'requested','requested_by':str(current.id),'requested_at':datetime.now(timezone.utc).isoformat(),'note':body.reason.strip()}
        s.save(current,row,'full_invoice_requested',body={**row['body'],'warehouse_composition':composition},state='awaiting_full_invoice')
        return {'id':id,'state':'awaiting_full_invoice'}

    @app.get('/api/documents/{id}/warehouse-composition/template')
    def template(id:UUID,current=dep):
        from collections import Counter
        from ..invoice_excel import xlsx
        source=current.connection.execute(text('SELECT * FROM bm_documents WHERE id=:id'),{'id':id}).mappings().first()
        if not source:raise HTTPException(404,'Накладная не найдена')
        current.require('marking.documents.view',organization_id=source['organization_id'],owner_id=source['owner_id'])
        if source['state']=='archived':current.require('documents.archive.view',organization_id=source['organization_id'],owner_id=source['owner_id'])
        current.require('marking.import.preview',organization_id=source['organization_id'],owner_id=source['owner_id'])
        counts=Counter()
        for line in source['lines']:
            if line.get('marked',True):counts[(line['pn'],line['gtin'])]+=line['quantity']*line['labels']
        data=xlsx({'Полная накладная':[['PN','GTIN','Количество']]+[[pn,gtin,qty] for (pn,gtin),qty in counts.items()]})
        return Response(data,media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',headers={'Content-Disposition':'attachment; filename="full-invoice-template.xlsx"'})

    async def upload(id,current,request,revision,source_revision,sheet_name,header_row,mapping,commit,expected_sha):
        c=current.connection
        source=c.execute(text('SELECT * FROM bm_documents WHERE id=:id'+(' FOR UPDATE' if commit else '')),{'id':id}).mappings().first()
        if not source:raise HTTPException(404,'Накладная не найдена')
        current.require('marking.documents.view',organization_id=source['organization_id'],owner_id=source['owner_id'])
        if source['state']=='archived':current.require('documents.archive.view',organization_id=source['organization_id'],owner_id=source['owner_id'])
        current.require('marking.documents.edit',organization_id=source['organization_id'],owner_id=source['owner_id'])
        current.require('marking.import.commit' if commit else 'marking.import.preview',organization_id=source['organization_id'],owner_id=source['owner_id'])
        row=c.execute(text("SELECT * FROM business_items WHERE source_id=:id AND kind='marking' AND state<>'archived'"+(' FOR UPDATE' if commit else '')),{'id':id}).mappings().first()
        if not row:raise HTTPException(409,'Процесс маркировки не найден или остановлен')
        row=dict(row);receipt=row['body'].get('warehouse_composition',{})
        replay=commit and receipt.get('state')=='ready' and receipt.get('submitted_revision')==revision and receipt.get('submitted_source_revision')==source_revision
        if not replay:
            if row['state']!='awaiting_full_invoice':raise HTTPException(409,'Логист должен вернуть накладную в БМ для дополнения состава')
            validate_source(current,row)
            if source['revision']!=source_revision or row['revision']!=revision:raise HTTPException(409,'Накладная изменилась. Обновите данные и предпросмотр.')
            if c.execute(text("SELECT EXISTS(SELECT 1 FROM warehouse_tasks WHERE source_id=:id AND state<>'cancelled')"),{'id':id}).scalar_one():raise HTTPException(409,'Склад уже работает с прежним составом. Обратитесь к логисту.')
        parts=[];size=0
        async for part in request.stream():
            size+=len(part)
            if size>10*1024*1024:raise HTTPException(413,'Полный Excel должен быть не больше 10 МБ')
            parts.append(part)
        data=b''.join(parts);digest=sha256(data).hexdigest()
        if commit and digest!=expected_sha:raise HTTPException(409,'Файл отличается от предпросмотра. Проверьте его заново.')
        if replay:
            if receipt['sha256']!=digest:raise HTTPException(409,'Повторный файл отличается от сохранённого полного состава')
            return {'id':id,'state':row['state'],'revision':source['revision'],'unmarked_lines':receipt['unmarked_lines'],'replayed':True}
        try:
            columns=json.loads(mapping) if mapping is not None else None
            result=await run_in_threadpool(parse,data,source['lines'],sheet_name=sheet_name,header_row=header_row,mapping=columns)
        except ValueError as error:raise HTTPException(422,str(error)) from None
        except Exception:raise HTTPException(422,'Не удалось прочитать XLSX. Сохраните книгу заново без формул.') from None
        if not commit:return {**result,'sha256':digest,'revision':row['revision'],'source_revision':source['revision']}
        if result['requires_mapping'] or result['errors']:raise HTTPException(422,{'message':'Полный состав не прошёл проверку','errors':result['errors'][:100]})
        s.idle(c,row['id'])
        original=[dict(v) for v in source['lines'] if v.get('marked',True)]
        lines=original+result['lines'];new_revision=source['revision']+1
        c.execute(text('UPDATE bm_documents SET lines=CAST(:lines AS jsonb),revision=:revision,updated_at=now() WHERE id=:id'),{'id':id,'lines':json.dumps(lines),'revision':new_revision})
        c.execute(text('UPDATE operation_targets SET revision=:revision WHERE id=:id'),{'id':id,'revision':new_revision})
        snapshot={k:source[k] for k in ('id','organization_id','owner_id','number','document_date','supplier','state','policy_snapshot')};snapshot.update(lines=lines,revision=new_revision)
        c.execute(text("INSERT INTO bm_document_versions(document_id,organization_id,revision,actor_id,actor_name,action,reason,snapshot) VALUES (:id,:org,:revision,:actor,:name,'edited',:reason,CAST(:snapshot AS jsonb))"),{'id':id,'org':source['organization_id'],'revision':new_revision,'actor':current.id,'name':current.user['display_name'],'reason':'Дополнен полный состав поставки после возврата из логистики','snapshot':json.dumps(snapshot,default=str)})
        file=s.artifact(current,row,'warehouse_invoice','full-invoice.xlsx',data,'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',{'sha256':digest,'unmarked_lines':len(result['lines']),'source_revision':new_revision})
        composition={**row['body']['warehouse_composition'],'state':'ready','submitted_revision':row['revision'],'submitted_source_revision':source['revision'],'completed_by':str(current.id),'completed_at':datetime.now(timezone.utc).isoformat(),'file_id':str(file),'sha256':digest,'unmarked_lines':len(result['lines'])}
        s.save(current,row,'full_invoice_received',body={**row['body'],'lines':lines,'source_revision':new_revision,'warehouse_composition':composition},state='warehouse_ready')
        return {'id':id,'state':'warehouse_ready','revision':new_revision,'unmarked_lines':len(result['lines'])}

    @app.post('/api/documents/{id}/warehouse-composition/preview')
    async def preview(id:UUID,request:Request,revision:int=Query(ge=1),source_revision:int=Query(ge=1),sheet_name:str|None=Query(None,max_length=100),header_row:int|None=Query(None,ge=0,le=20030),mapping:str|None=Query(None,max_length=2000),current=dep):
        return await upload(id,current,request,revision,source_revision,sheet_name,header_row,mapping,False,None)

    @app.post('/api/documents/{id}/warehouse-composition/commit')
    async def commit(id:UUID,request:Request,revision:int=Query(ge=1),source_revision:int=Query(ge=1),sha256:str=Query(pattern='^[a-f0-9]{64}$'),sheet_name:str|None=Query(None,max_length=100),header_row:int|None=Query(None,ge=0,le=20030),mapping:str|None=Query(None,max_length=2000),current=dep):
        return await upload(id,current,request,revision,source_revision,sheet_name,header_row,mapping,True,sha256)

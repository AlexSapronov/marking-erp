"""Independent invoice commands with immutable retry receipts."""
from datetime import date
from hashlib import sha256
import json
from uuid import UUID,uuid5
from fastapi import Depends,HTTPException
from pydantic import Field,StrictBool,StrictInt
from sqlalchemy import text
from ..auth_routes import Body


class Entry(Body):
    id:UUID
    revision:StrictInt=Field(ge=1)


class Batch(Body):
    request_id:UUID
    organization_id:UUID
    action:str=Field(pattern='^(prepare|start|report)$')
    entries:list[Entry]=Field(min_length=1,max_length=100)
    confirmed:StrictBool=False
    production_date:date|None=None


def install(app,actor):
    @app.post('/api/bm-bulk')
    def execute(body:Batch,current=Depends(actor,scope='function')):
        from ..document_routes import Change
        from .marking import Start,Step
        c=current.connection
        if len({v.id for v in body.entries})!=len(body.entries):raise HTTPException(422,'Накладные повторяются')
        if not body.confirmed:raise HTTPException(422,'Подтвердите выбранные накладные и действие')
        docs={}
        for entry in body.entries:
            doc=c.execute(text('SELECT * FROM bm_documents WHERE id=:id'),{'id':entry.id}).mappings().first()
            if not doc:raise HTTPException(404,'Накладная не найдена')
            assigned=c.execute(text('SELECT EXISTS(SELECT 1 FROM operation_assignments WHERE target_id=:id AND user_id=:user)'),{'id':entry.id,'user':current.id}).scalar_one()
            current.require('marking.documents.view',organization_id=doc['organization_id'],owner_id=doc['owner_id'],assigned=assigned)
            if doc['organization_id']!=body.organization_id:raise HTTPException(422,'Выберите одну организацию')
            docs[entry.id]=doc
        fingerprint=sha256(json.dumps(body.model_dump(mode='json'),sort_keys=True).encode()).hexdigest()
        old=c.execute(text('SELECT * FROM bm_bulk_receipts WHERE id=:id'),{'id':body.request_id}).mappings().first()
        if old:
            if old['fingerprint']!=fingerprint or old['actor_id']!=current.id:raise HTTPException(409,'Запрос массовой операции отличается')
            return {'results':old['results'],'replayed':True}
        def endpoint(path):return next(r.endpoint for r in app.routes if getattr(r,'path',None)==path and 'POST' in getattr(r,'methods',set()))
        results=[]
        for entry in body.entries:
            try:
                with c.begin_nested():
                    doc=docs[entry.id]
                    if doc['revision']!=entry.revision:raise HTTPException(409,'Накладная изменена — обновите список')
                    if body.action=='prepare':result=endpoint('/api/documents/{id}/{action}')(entry.id,'prepare',Change(revision=entry.revision),current)
                    elif body.action=='start':result=endpoint('/api/marking/start')(Start(source_id=entry.id,revision=entry.revision,request_id=uuid5(body.request_id,str(entry.id)),confirmed=True),current)
                    else:
                        process=c.execute(text("SELECT id,revision FROM business_items WHERE kind='marking' AND source_id=:id AND state<>'archived'"),{'id':entry.id}).mappings().first()
                        if not process:raise HTTPException(409,'Маркировка ещё не начата')
                        result=endpoint('/api/business/{id}/{action}')(process['id'],'report',Step(revision=process['revision'],confirmed=True,production_date=body.production_date),current)
                    results.append({'id':str(entry.id),'number':doc['number'],'ok':True})
            except HTTPException as error:results.append({'id':str(entry.id),'number':docs[entry.id]['number'],'ok':False,'message':str(error.detail)})
            except ValueError:results.append({'id':str(entry.id),'number':docs[entry.id]['number'],'ok':False,'message':'Проверьте подключение и состояние операции'})
        c.execute(text('INSERT INTO bm_bulk_receipts(id,organization_id,actor_id,fingerprint,results) VALUES (:id,:org,:actor,:fp,CAST(:results AS jsonb))'),{'id':body.request_id,'org':body.organization_id,'actor':current.id,'fp':fingerprint,'results':json.dumps(results)})
        return {'results':results,'replayed':False}

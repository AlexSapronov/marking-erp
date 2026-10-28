"""Durable, scoped NK bulk commands and explicit import preparation."""
from hashlib import sha256
import json
from uuid import UUID
from importlib.resources import files
from fastapi import Depends, HTTPException
from pydantic import Field, StrictInt
from ..auth_routes import Body
from . import store as s
from .catalog_policy import rules
from .errors import RemoteError


class Entry(Body):
    id: UUID
    revision: StrictInt = Field(ge=1)


class Batch(Body):
    organization_id: UUID
    request_id: UUID
    action: str = Field(pattern='^(gtin|save|moderate|publish|refresh)$')
    entries: list[Entry] = Field(min_length=1, max_length=100)
    agreement: bool = False
    reason: str = Field(default='', max_length=1000)


class Prepare(Body):
    revision: StrictInt = Field(ge=1)
    # Explicit per-TN VED choice: never guess between component kinds.
    kinds: dict[str, str] = Field(max_length=100)


def install(app, actor, settings):
    from .nk import perform_action, Action
    dep = Depends(actor, scope='function')

    @app.post('/api/catalog-bulk')
    def bulk(body: Batch, current=dep):
        current.require('nk.cards.view', organization_id=body.organization_id, owner_id=current.id, assigned=True)
        c = current.connection
        from sqlalchemy import text
        fingerprint = sha256(json.dumps(body.model_dump(mode='json'),sort_keys=True).encode()).hexdigest()
        old = c.execute(text('SELECT * FROM business_items WHERE id=:id'), {'id':body.request_id}).mappings().first()
        if old:
            s.require(current, old)
            if old['organization_id']!=body.organization_id or old['kind']!='import' or old['body'].get('request_sha256')!=fingerprint: raise HTTPException(409,'Запрос массовой операции отличается')
            for entry in body.entries: s.get(current, entry.id, 'card')
            return old['body']['results']
        policy = rules(c, body.organization_id)
        if len(body.entries)>policy['values']['nk.bulk.batch_size']: raise HTTPException(422,'Превышен настроенный размер пакета НК')
        if len({e.id for e in body.entries})!=len(body.entries): raise HTTPException(422,'Карточки в пакете повторяются')
        permission={'gtin':'nk.gtin.generate','save':'nk.cards.edit','moderate':'nk.cards.moderate','publish':'nk.cards.publish','refresh':'nk.cards.view'}[body.action]
        for entry in body.entries:
            row=s.get(current,entry.id,'card',True,permission)
            if row['organization_id']!=body.organization_id: raise HTTPException(422,'В пакете разные организации')
        result=[]
        for entry in body.entries:
            try:
                with c.begin_nested():
                    response=perform_action(current,entry.id,body.action,Action(revision=entry.revision,agreement=body.agreement,reason=body.reason),settings)
                    result.append({'id':str(entry.id),'ok':True,'operation_id':str(response['operation_id']) if response.get('operation_id') else None})
            except HTTPException as error:
                result.append({'id':str(entry.id),'ok':False,'message':error.detail if isinstance(error.detail,str) else error.detail.get('message','Проверьте карточку')})
            except RemoteError as error:
                result.append({'id':str(entry.id),'ok':False,'message':str(error)})
        receipt=s.create(current,'import','Массовое действие НК · '+body.action,{'bulk_action':body.action,'request_sha256':fingerprint,'results':result,'policy':policy},body.organization_id,id=body.request_id,state='done')
        if current.grants().get('nk.cards.view')=='assigned':
            c.execute(text('INSERT INTO operation_assignments(target_id,user_id) VALUES (:id,:user)'),{'id':receipt['id'],'user':current.id})
        return result

    @app.post('/api/catalog-import/{id}/prepare')
    def prepare(id: UUID, body: Prepare, current=dep):
        row=s.get(current,id,'import',True,'nk.import.commit');s.version(row,body.revision)
        if row['state']!='draft' or row['body'].get('card_ids'):raise HTTPException(409,'Подготовка доступна только до создания карточек')
        kinds={k['id']:k for k in json.loads(files('marking_erp.data').joinpath('presets/product-kinds.json').read_text())['kinds']}
        for tnved,key in body.kinds.items():
            if key not in kinds or kinds[key]['tnved']!=tnved:raise HTTPException(422,'Вид товара не соответствует ТН ВЭД')
        rows=[]
        for source in row['body']['rows']:
            d={**source['data'],'attributes':dict(source['data'].get('attributes',{}))};k=kinds.get(body.kinds.get(d['tnved']))
            if k:
                if d.get('category_id') not in (0,int(k['category_id'])):raise HTTPException(422,'Категория строки отличается от выбранного вида товара')
                d['category_id']=int(k['category_id'])
                defaults={'12':k['nk_kind'],'24003':k['variable_type'],'13914':d['pn'],'13914::unit':'Артикул'}
                if k['other_kind']:defaults['23828']=k['other_kind']
                for key,value in defaults.items():
                    if not d['attributes'].get(key):d['attributes'][key]=value
            rows.append({**source,'data':d})
        return s.save(current,row,'import_prepared',body={**row['body'],'rows':rows,'selected_kinds':body.kinds})

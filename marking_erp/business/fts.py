"""FTS workspace: prepare dossier; confirm circulation through live evidence.

No undocumented import endpoint or document schema is invented. Submission stays in LK.
"""
import base64,json
from datetime import date,datetime,timezone
from uuid import UUID
from fastapi import Depends,HTTPException
from pydantic import Field,StrictInt
from sqlalchemy import text
from ..auth_routes import Body
from . import store as s
from .connections import load
from .live import Live
from .nk import workbook

class Prepare(Body):
    organization_id:UUID
    request_id:UUID
    atks:list[UUID]=Field(min_length=1,max_length=100)
    declaration_number:str=Field(min_length=1,max_length=100)
    declaration_date:date
    country_of_origin:str=Field(min_length=2,max_length=100)
    conformity_document:str=Field(min_length=1,max_length=500)

class Confirm(Body):
    revision:StrictInt=Field(ge=1)
    external_document_id:UUID


def strings(value,depth=0):
    if depth>20:return set()
    if isinstance(value,dict):return set().union(*(strings(v,depth+1) for v in value.values())) if value else set()
    if isinstance(value,list):return set().union(*(strings(v,depth+1) for v in value)) if value else set()
    if not isinstance(value,str):return set()
    out={value}
    if len(value)<=1048576:
        for raw in [value]:
            try:out|=strings(json.loads(raw),depth+1)
            except (ValueError,TypeError):
                try:out|=strings(json.loads(base64.b64decode(raw,validate=True)),depth+1)
                except (ValueError,TypeError,UnicodeError):pass
    return out

def install_fts(app,actor,settings):
    dep=Depends(actor,scope='function')
    @app.post('/api/fts')
    def prepare(body:Prepare,current=dep):
        current.require('logistics.fts.prepare',organization_id=body.organization_id,owner_id=current.id)
        c=current.connection;existing=c.execute(text('SELECT * FROM business_items WHERE id=:id'),{'id':body.request_id}).mappings().first()
        if existing:
            s.require(current,existing,'logistics.fts.prepare')
            if existing['kind']!='fts' or existing['body']['atks']!=sorted(map(str,set(body.atks))) or existing['body']['declaration_number']!=body.declaration_number:raise HTTPException(409,'Идентификатор запроса уже использован')
            return dict(existing)
        codes=[];atks=[]
        for id in sorted(set(body.atks)):
            row=s.get(current,id,'atk',True,'logistics.fts.prepare')
            if row['organization_id']!=body.organization_id or row['state'] not in ('ready','transferred'):raise HTTPException(409,'Выберите зарегистрированные АТК своей организации')
            if c.execute(text("SELECT EXISTS(SELECT 1 FROM business_items WHERE kind='transform' AND body->>'atk_item'=:id AND state IN ('processing','awaiting_approval'))"),{'id':str(id)}).scalar_one():raise HTTPException(409,'По АТК есть незавершённая трансформация')
            atks.append(row['body']['atk']);values=c.execute(text('SELECT id,cis,pn,gtin,tnved,quantity FROM business_codes WHERE organization_id=:org AND id=ANY(CAST(:ids AS uuid[]))'),{'org':body.organization_id,'ids':row['body']['codes']}).mappings().all()
            codes.extend({**r,'id':str(r['id']),'atk':row['body']['atk']} for r in values)
        if len({r['cis'] for r in codes})!=len(codes):raise HTTPException(422,'Выбранные АТК содержат повторяющиеся КМ')
        if body.declaration_date>date.today():raise HTTPException(422,'Дата декларации не может быть в будущем')
        values=body.model_dump(mode='json',exclude={'organization_id','request_id'});values.update(atks=sorted(map(str,set(body.atks))),atk_codes=atks,codes=codes)
        row=s.create(current,'fts','ФТС · '+body.declaration_number,values,body.organization_id,id=body.request_id,state='prepared')
        data=workbook([[r['pn'],r['gtin'],r['tnved'],r['quantity'],r['cis'],r['atk'],body.declaration_number,body.country_of_origin,body.conformity_document] for r in codes],['PN','GTIN','ТН ВЭД','Количество','КМ','АТК','ДТ','Страна происхождения','Разрешительный документ'])
        s.artifact(current,row,'fts','FTS-dossier.xlsx',data,'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',{'declaration_number':body.declaration_number,'codes':[r['cis'] for r in codes]})
        return row
    @app.post('/api/fts/{id}/confirm')
    def confirm(id:UUID,body:Confirm,current=dep):
        row=s.get(current,id,'fts',True,'logistics.fts.submit');s.version(row,body.revision);c=current.connection
        config=load(c,settings,row['organization_id']);inn=c.execute(text('SELECT inn FROM organizations WHERE id=:id'),{'id':row['organization_id']}).scalar_one();live=Live(config,inn)
        document=live.call('GET','/api/v4/true-api/doc/'+str(body.external_document_id)+'/info',{'pg':'radio','body':'true','content':'true'})
        if not isinstance(document,list) or len(document)!=1 or document[0].get('number')!=str(body.external_document_id) or document[0].get('status')!='CHECKED_OK' or document[0].get('errors') or document[0].get('commonErrors'):raise HTTPException(409,'ЧЗ не подтвердил успешную обработку указанного документа')
        values=strings(document)
        if row['body']['declaration_number'] not in values:raise HTTPException(409,'Документ ЧЗ не подтвердил указанную декларацию на товары')
        expected={r['cis'] for r in row['body']['codes']};found={v[:38] for v in values if v[:38] in expected}
        if found!=expected and not set(row['body']['atk_codes'])<=values:raise HTTPException(409,'Не подтверждена связь документа ЧЗ с выбранными КМ/АТК')
        info=live.verify(row['body']['codes'],'INTRODUCED')
        if any(r.get('parent')!=code['atk'] for code in row['body']['codes'] for r in [info[code['cis']]]):raise HTTPException(409,'Состав АТК изменился. Проверьте пакет ФТС.')
        proof=json.dumps(document,ensure_ascii=False).encode();s.artifact(current,row,'fts_receipt','FTS-receipt.json',proof,'application/json',{'external_document_id':str(body.external_document_id)})
        return s.save(current,row,'circulation_verified',body={**row['body'],'external_document_id':str(body.external_document_id),'verified_at':datetime.now(timezone.utc).isoformat(),'verification':'codes_introduced_and_document_linked'},state='circulation_confirmed')

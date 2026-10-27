"""Official radio KM replacement, True API v747 §4.1.19 (LK_REMARK)."""
from datetime import date
from calendar import monthrange
from hashlib import sha256
import json
from uuid import UUID
from fastapi import Depends,HTTPException
from pydantic import Field,StrictBool,StrictInt
from sqlalchemy import text
from ..auth_routes import Body
from . import store as s
from .connections import load
from .live import Live
from .marking import organization,order_payload,rows

class Create(Body):
    organization_id:UUID
    request_id:UUID
    old_code_id:UUID
    reason:str=Field(min_length=8,max_length=1000)
    confirmed:StrictBool=False
class Step(Body):
    revision:StrictInt=Field(ge=1)
    confirmed:StrictBool=False
    production_date:date|None=None
    remarking_date:date|None=None


def quantity(info):
    value=info.get('quantityInPack')
    if isinstance(value,bool) or not str(value).isdigit() or int(value)<1:raise HTTPException(409,'ЧЗ не подтвердил количество исходного КМ')
    return int(value)

def verify_old(live,code):
    info=live.verify([code],'INTRODUCED')[code['cis']]
    if info.get('packageType')!='UNIT':raise HTTPException(409,'Этот сценарий предназначен для единицы товара (UNIT)')
    if quantity(info)!=code['quantity']:raise HTTPException(409,'Количество исходного КМ изменилось в ЧЗ')
    return info

def official_payload(inn,old,new,day):
    return {'participant_inn':inn,'remarking_date':day.isoformat(),'remarking_cause':'KM_SPOILED',
            'products':[{'last_uin':old,'new_uin':new}]}

def validate_dispatch(live,body):
    old=live.info([body['products'][0]['last_uin']])[body['products'][0]['last_uin']]
    new_cis=body['products'][0]['new_uin'];new=live.info([new_cis])[new_cis]
    if old.get('ownerInn')!=live.inn or old.get('status')!='INTRODUCED' or old.get('statusEx') not in (None,'EMPTY'):raise HTTPException(409,'Исходный КМ изменился после подготовки перемаркировки')
    if new.get('ownerInn')!=live.inn or new.get('status')!='APPLIED' or new.get('statusEx') not in (None,'EMPTY') or new.get('emissionType')!='REMARK' or new.get('parent'):raise HTTPException(409,'Новый КМ не готов к официальной перемаркировке')
    if old.get('gtin')!=new.get('gtin') or old.get('packageType')!='UNIT' or new.get('packageType')!='UNIT' or quantity(old)!=quantity(new):raise HTTPException(409,'Товар, упаковка или количество предыдущего и нового КМ различаются')

def verify_result(live,body,expected_quantity=None):
    entry=body['products'][0];values=live.info([entry['last_uin'],entry['new_uin']]);old=values[entry['last_uin']];new=values[entry['new_uin']]
    # A successful document alone is insufficient; the old code must actually retire.
    if old.get('status')!='WRITTEN_OFF' or old.get('statusEx')!='REMARK_RETIRED':return False
    if new.get('status')!='INTRODUCED' or new.get('statusEx') not in (None,'EMPTY') or new.get('ownerInn')!=live.inn or new.get('gtin')!=old.get('gtin'):return False
    expected=quantity(old) if expected_quantity is None else expected_quantity
    return quantity(new)==expected and (old.get('quantityInPack') is None or quantity(old)==expected)


def install_remarking(app,actor,settings):
    dep=Depends(actor,scope='function')
    @app.post('/api/remarks')
    def create(body:Create,current=dep):
        c=current.connection;current.require('labels.remarking.create',organization_id=body.organization_id,owner_id=current.id)
        old=c.execute(text('SELECT * FROM business_codes WHERE id=:id AND organization_id=:org FOR UPDATE'),{'id':body.old_code_id,'org':body.organization_id}).mappings().first()
        if not old:raise HTTPException(404,'Исходный КМ не найден')
        prior=c.execute(text('SELECT * FROM business_items WHERE id=:id'),{'id':body.request_id}).mappings().first()
        fingerprint=sha256(body.model_dump_json().encode()).hexdigest()
        if prior:
            s.require(current,prior,'labels.remarking.create')
            if prior['kind']!='remark' or prior['body'].get('request_sha256')!=fingerprint:raise HTTPException(409,'Идентификатор запроса уже использован')
            return dict(prior)
        if not body.confirmed or len(body.reason.strip())<8:raise HTTPException(422,'Подтвердите платную эмиссию замены и укажите причину')
        if old['item_id']:s.require(current,s.item(c,old['item_id']),'labels.remarking.create')
        if c.execute(text("SELECT EXISTS(SELECT 1 FROM business_items i JOIN business_links l ON l.item_id=i.id JOIN external_operations o ON o.id=l.operation_id WHERE i.organization_id=:org AND i.kind IN ('remark','split') AND COALESCE(i.body->>'old_cis',i.body->>'source_cis')=:cis AND (o.state NOT IN ('confirmed','rejected','cancelled_before_dispatch') OR (o.state='confirmed' AND NOT l.applied)))"),{'org':body.organization_id,'cis':old['cis']}).scalar_one():raise HTTPException(409,'Есть незавершённая внешняя операция по этому КМ, включая архив. Сначала сверьте её результат.')
        if c.execute(text("SELECT EXISTS(SELECT 1 FROM reprint_requests WHERE code_id=:id AND state IN ('prepared','printed'))"),{'id':old['id']}).scalar_one():raise HTTPException(409,'Сначала завершите или отмените перепечать исходной этикетки')
        if old['state']!='applied':raise HTTPException(409,'Исходный КМ ещё не нанесён или уже заменён')
        if c.execute(text("SELECT EXISTS(SELECT 1 FROM business_items WHERE organization_id=:org AND ((kind='remark' AND body->>'old_cis'=:cis AND state NOT IN ('done','archived')) OR (kind='split' AND body->>'source_cis'=:cis AND state NOT IN ('applied','archived'))))"),{'org':body.organization_id,'cis':old['cis']}).scalar_one():raise HTTPException(409,'По этому КМ уже есть незавершённая операция')
        original=s.item(c,old['item_id']) if old['item_id'] else None
        source_id=original['source_id'] if original else None
        if source_id and c.execute(text("SELECT EXISTS(SELECT 1 FROM warehouse_tasks WHERE source_id=:id AND state NOT IN ('closed','cancelled'))"),{'id':source_id}).scalar_one():raise HTTPException(409,'Сначала завершите или отмените активную складскую сверку: её ожидаемый состав менять нельзя')
        config=load(c,settings,body.organization_id);live=Live(config,organization(c,body.organization_id));verify_old(live,old)
        row=s.create(current,'remark','Замена · '+old['pn'],{'old_code_id':str(old['id']),'old_cis':old['cis'],'gtin':old['gtin'],'pn':old['pn'],'tnved':old['tnved'],'quantity':old['quantity'],'line_index':old['line_index'],'reason':body.reason.strip(),'request_sha256':fingerprint},body.organization_id,source=source_id,id=body.request_id,state='ordering')
        payload=order_payload(old['gtin'],1,config,row['id']);payload['attributes']['releaseMethodType']='REMARK'
        s.queue(current,row,'remark.order',payload);return row
    @app.post('/api/remarks/{id}/{action}')
    def action(id:UUID,action:str,body:Step,current=dep):
        row=s.get(current,id,'remark',True,'labels.remarking.submit');s.version(row,body.revision);s.idle(current.connection,id);c=current.connection
        codes=rows(c,id);config=load(c,settings,row['organization_id']);live=Live(config,organization(c,row['organization_id']))
        if not body.confirmed:raise HTTPException(422,'Требуется подтверждение действия')
        if action=='applied':
            if row['state']!='labels_ready':raise HTTPException(409,'Сначала получите новый КМ и этикетку')
            return s.save(current,row,'replacement_label_applied',state='application_confirmed')
        if action=='report':
            if (row['state']!='application_confirmed' and not (row['state']=='rejected' and row['body'].get('last_rejected_step')=='remark.report')) or len(codes)!=1:raise HTTPException(409,'Сначала подтвердите нанесение новой этикетки')
            if not body.production_date or body.production_date>date.today():raise HTTPException(422,'Укажите фактическую дату производства')
            live.verify(codes,'EMITTED');payload={'productGroup':'radio','sntins':[bytes(codes[0]['payload']).decode()],'attributes':{'productionDate':body.production_date.isoformat()}}
            for key,target in [('kpp','kpp'),('fias_id','fiasId')]:
                if config.get(key):payload['attributes'][target]=config[key]
            s.queue(current,row,'remark.report',payload);return s.save(current,row,'replacement_report_queued',state='reporting')
        if action=='submit':
            if (row['state']!='applied' and not (row['state']=='rejected' and row['body'].get('last_rejected_step')=='remark.submit')) or len(codes)!=1:raise HTTPException(409,'Дождитесь подтверждённого отчёта о нанесении нового КМ')
            if not body.remarking_date or not date(date.today().year-5,date.today().month,min(date.today().day,monthrange(date.today().year-5,date.today().month)[1]))<=body.remarking_date<=date.today():raise HTTPException(422,'Проверьте дату перемаркировки')
            payload=official_payload(live.inn,row['body']['old_cis'],codes[0]['cis'],body.remarking_date);payload['products'][0]['tnved_10']=row['body']['tnved']
            validate_dispatch(live,payload);s.queue(current,row,'remark.submit',payload)
            return s.save(current,row,'official_remark_queued',body={**row['body'],'new_cis':codes[0]['cis'],'remarking_date':body.remarking_date.isoformat()},state='remarking')
        raise HTTPException(404,'Действие не найдено')

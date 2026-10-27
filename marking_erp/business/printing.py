"""Exact saved PDF printing. Claim once; ambiguous spool outcomes never auto-retry."""
import base64
from datetime import datetime,timezone
from hashlib import sha256
from io import BytesIO
import json,secrets,zipfile
from uuid import UUID,uuid4
from fastapi import Depends,HTTPException,Request
from pydantic import Field,StrictInt
from sqlalchemy import text
from .. import auth,gs1
from ..auth_routes import Body
from ..policy import snapshot
from . import store as s

class Reprint(Body):
    request_id:UUID
    reason:str=Field(default='',max_length=1000)
    copies:StrictInt=Field(default=1,ge=1,le=10)
class Scan(Body):
    request_id:UUID
    raw:str=Field(min_length=1,max_length=1024)
class Send(Body):
    request_id:UUID
    printer_id:UUID
    reprint_id:UUID|None=None
class PrintReceipt(Body):
    job_id:UUID
    lease:str=Field(min_length=20,max_length=128)
    pdf_sha256:str=Field(pattern='^[a-f0-9]{64}$')
    outcome:str=Field(pattern='^(spooled|unknown)$')
    spool_reference:str=Field(default='',max_length=200)
class Resolve(Body):
    outcome:str=Field(pattern='^(spooled|cancelled)$')
    reason:str=Field(min_length=8,max_length=1000)


def context(current,row):
    assigned=current.connection.execute(text('''SELECT EXISTS(SELECT 1 FROM warehouse_tasks w JOIN operation_assignments a ON a.target_id=w.id
        WHERE w.source_id=:source AND w.state<>'cancelled' AND a.user_id=:user)'''),{'source':row['source_id'],'user':current.id}).scalar_one()
    return dict(organization_id=row['organization_id'],owner_id=row['owner_id'],assigned=assigned)

def event(c,id,action,*,actor=None,device=None,body=None):
    c.execute(text('INSERT INTO print_events(job_id,actor_id,device_id,action,body) VALUES (:id,:actor,:device,:action,CAST(:body AS jsonb))'),{'id':id,'actor':actor,'device':device,'action':action,'body':json.dumps(body or {},default=str)})
    auth.audit(c,actor,'labels.'+action,id,body or {})

def digest(body):return sha256(json.dumps(body,sort_keys=True,default=str).encode()).hexdigest()

def code_access(current,id,permission='labels.reprint'):
    code=current.connection.execute(text('SELECT * FROM business_codes WHERE id=:id FOR UPDATE'),{'id':id}).mappings().first()
    if not code or not code['item_id']:raise HTTPException(404,'КМ не найден')
    row=s.item(current.connection,code['item_id'])
    current.require(permission,**context(current,row))
    return dict(code),row

def usable(code,row):
    if row['state']=='archived' or row['body'].get('codes_released') or code['state'] not in ('reserved','emitted','applied'):raise HTTPException(409,'КМ остановлен или больше не принадлежит документу')

def exact_pdf(c,code,row):
    saved=c.execute(text("SELECT * FROM business_artifacts WHERE item_id=:id AND kind='labels' ORDER BY version DESC LIMIT 1"),{'id':row['id']}).mappings().first()
    if not saved:raise HTTPException(409,'Нет сохранённой версии этикетки')
    payload_sha=sha256(bytes(code['payload'])).hexdigest()
    index=next((i for i,v in enumerate(saved['snapshot']['codes']) if v['cis']==code['cis'] and v['sha256']==payload_sha and v['quantity']==code['quantity'] and v['pn']==code['pn']),None)
    if index is None:raise HTTPException(409,'Сохранённая этикетка не соответствует текущему КМ')
    if sha256(bytes(saved['body'])).hexdigest()!=saved['sha256']:raise HTTPException(503,'Архив этикеток повреждён')
    with zipfile.ZipFile(BytesIO(bytes(saved['body']))) as archive:return archive.read(f'{index+1:05d}.pdf')

def reprint_access(current,id):
    row=current.connection.execute(text('SELECT * FROM reprint_requests WHERE id=:id FOR UPDATE'),{'id':id}).mappings().first()
    if not row:raise HTTPException(404,'Задание перепечати не найдено')
    code,item=code_access(current,row['code_id'])
    # A fresh administrator can recover a request left by an absent/disabled employee.
    if row['actor_id']!=current.id:current.require('integrations.manage',organization_id=row['organization_id'],administrative=True)
    return dict(row),code,item

def current_payload(request,code,item):
    usable(code,item)
    if sha256(bytes(code['payload'])).hexdigest()!=request['payload_sha256']:raise HTTPException(409,'Версия КМ изменилась. Прежнюю этикетку использовать нельзя.')

def public(row):return {k:v for k,v in row.items() if k not in ('request_sha256','lease_hash','token_hash')}

def register_printer(c,org,name,printer_name):
    if not name.strip() or not printer_name.strip() or max(len(name),len(printer_name))>200:raise ValueError('Printer name required')
    token=secrets.token_urlsafe(48);id=uuid4()
    c.execute(text('INSERT INTO printer_devices(id,organization_id,name,printer_name,token_hash) VALUES (:id,:org,:name,:printer,:hash)'),{'id':id,'org':org,'name':name.strip(),'printer':printer_name.strip(),'hash':sha256(token.encode()).hexdigest()})
    auth.audit(c,None,'printer.provisioned',id,{'organization_id':str(org),'name':name})
    return id,token

def device_auth(c,token):
    if not token or len(token)>128:raise HTTPException(401,'Требуется токен агента печати')
    row=c.execute(text('''SELECT d.* FROM printer_devices d JOIN organizations o ON o.id=d.organization_id
        WHERE d.token_hash=:hash AND d.enabled AND o.enabled FOR UPDATE OF d'''),{'hash':sha256(token.encode()).hexdigest()}).mappings().first()
    if not row:raise HTTPException(401,'Агент печати отключён')
    c.execute(text('UPDATE printer_devices SET last_seen_at=now() WHERE id=:id'),{'id':row['id']})
    return row

def authorize_job(c,job):
    user=c.execute(text('SELECT * FROM users WHERE id=:id AND enabled'),{'id':job['actor_id']}).mappings().first()
    if not user:raise HTTPException(403,'Инициатор печати отключён')
    current=auth.Actor(c,dict(user),'printer',None)
    file=c.execute(text('SELECT * FROM business_artifacts WHERE id=:id'),{'id':job['artifact_id']}).mappings().one()
    item=s.item(c,file['item_id'])
    current.require('labels.print.confirm',**context(current,item))
    if job['reprint_id']:
        request,code,item=reprint_access(current,job['reprint_id']);current_payload(request,code,item)
        if request['state']!='prepared':raise HTTPException(409,'Задание перепечати уже обработано')
    else:
        current.require('labels.print.confirm',**context(current,item));current.require('labels.remarking.create' if item['kind']=='remark' else 'marking.labels.download' if item['kind']=='marking' else 'split.operations.view',**s.context(current,item))
        if item['state']=='archived' or item['body'].get('codes_released'):raise HTTPException(409,'Документ остановлен')
        latest=c.execute(text("SELECT id FROM business_artifacts WHERE item_id=:item AND kind='labels_pdf' ORDER BY version DESC LIMIT 1"),{'item':item['id']}).scalar_one_or_none()
        if c.execute(text("SELECT EXISTS(SELECT 1 FROM business_codes WHERE item_id=:id AND state='withdrawn')"),{'id':item['id']}).scalar_one():raise HTTPException(409,'В комплекте есть заменённые КМ')
        if latest!=file['id']:raise HTTPException(409,'Версия этикеток изменилась')
    from ..incidents import enforce
    cises=c.execute(text('SELECT cis FROM business_codes WHERE item_id=:id'),{'id':item['id']}).scalars().all()
    enforce(c,item['organization_id'],'reprint',cises=cises,targets=[item['id'],item['source_id']])
    if sha256(bytes(file['body'])).hexdigest()!=file['sha256']:raise HTTPException(503,'PDF повреждён')
    return file


def install_printing(app,actor,engine):
    dep=Depends(actor,scope='function')
    @app.post('/api/business-codes/{id}/reprint')
    def prepare(id:UUID,body:Reprint,current=dep):
        code,item=code_access(current,id);c=current.connection
        fingerprint=digest({'code_id':id,**body.model_dump()})
        old=c.execute(text('SELECT * FROM reprint_requests WHERE id=:id'),{'id':body.request_id}).mappings().first()
        if old:
            if old['actor_id']!=current.id or old['request_sha256']!=fingerprint:raise HTTPException(409,'Идентификатор запроса уже использован')
            return {**public(dict(old)),'file_id':old['artifact_id']}
        if c.execute(text("SELECT EXISTS(SELECT 1 FROM business_items WHERE kind='remark' AND body->>'old_code_id'=:id AND state NOT IN ('done','archived'))"),{'id':str(id)}).scalar_one():raise HTTPException(409,'По этому КМ выполняется официальная замена; перепечать старой этикетки остановлена')
        from ..incidents import enforce
        enforce(c,code['organization_id'],'reprint',cises=[code['cis']],targets=[item['id'],item['source_id']])
        usable(code,item);policy=snapshot(c,code['organization_id']);rules=policy['values']
        if rules['labels.reprint.reason_required'] and len(body.reason.strip())<8:raise HTTPException(422,'Укажите содержательную причину перепечати (от 8 символов)')
        if body.copies>rules['labels.reprint.max_copies']:raise HTTPException(422,'Превышен разрешённый максимум копий')
        if c.execute(text("SELECT EXISTS(SELECT 1 FROM reprint_requests WHERE code_id=:code AND state IN ('prepared','printed'))"),{'code':id}).scalar_one():raise HTTPException(409,'Сначала завершите или отмените предыдущую перепечать этого КМ')
        pdf=exact_pdf(c,code,item);hash=sha256(bytes(code['payload'])).hexdigest()
        file=s.artifact(current,item,'reprint','label-58x40.pdf',pdf,'application/pdf',{'code_id':str(id),'payload_sha256':hash,'pn':code['pn'],'quantity':code['quantity'],'request_id':str(body.request_id),'copies':body.copies})
        c.execute(text('''INSERT INTO reprint_requests(id,organization_id,actor_id,code_id,artifact_id,reason,copies,payload_sha256,policy,request_sha256)
            VALUES (:id,:org,:actor,:code,:file,:reason,:copies,:sha,CAST(:policy AS jsonb),:request)'''),{'id':body.request_id,'org':code['organization_id'],'actor':current.id,'code':id,'file':file,'reason':body.reason.strip(),'copies':body.copies,'sha':hash,'policy':json.dumps(policy),'request':fingerprint})
        event(c,body.request_id,'reprint_prepared',actor=current.id,body={'reason':body.reason.strip(),'copies':body.copies,'control_scan':rules['labels.reprint.control_scan']})
        return {'id':body.request_id,'file_id':file,'copies':body.copies,'state':'prepared','scanned_copies':0,'policy':policy}
    @app.get('/api/reprints/{id}')
    def detail(id:UUID,current=dep):
        row,_,_=reprint_access(current,id);return public(row)
    @app.get('/api/reprints')
    def requests(organization_id:UUID,all_users:bool=False,current=dep):
        if all_users:current.require('integrations.manage',organization_id=organization_id,administrative=True)
        if organization_id not in current.organizations():raise HTTPException(403,'Нет доступа')
        rows=current.connection.execute(text('''SELECT r.*,c.pn,c.cis FROM reprint_requests r JOIN business_codes c ON c.id=r.code_id
            WHERE r.organization_id=:org AND (:all_users OR r.actor_id=:actor) ORDER BY r.created_at DESC,r.id DESC LIMIT 100'''),{'org':organization_id,'actor':current.id,'all_users':all_users}).mappings().all()
        out=[]
        for r in rows:
            try:code_access(current,r['code_id'])
            except HTTPException:continue
            out.append(public(dict(r)))
        return {'items':out}
    @app.post('/api/reprints/{id}/printed')
    def printed(id:UUID,current=dep):
        row,code,item=reprint_access(current,id);current.require('labels.print.confirm',**context(current,item))
        if row['state'] in ('printed','completed'):return public(row)
        current_payload(row,code,item)
        if row['state']!='prepared':raise HTTPException(409,'Перепечать отменена')
        job=current.connection.execute(text('SELECT state FROM print_jobs WHERE reprint_id=:id'),{'id':id}).scalar_one_or_none()
        if job and job!='spooled':raise HTTPException(409,'Сначала проверьте результат агента печати')
        finish=not row['policy']['values']['labels.reprint.control_scan']
        current.connection.execute(text("UPDATE reprint_requests SET state=:state,completed_at=:time WHERE id=:id"),{'id':id,'state':'completed' if finish else 'printed','time':datetime.now(timezone.utc) if finish else None})
        event(current.connection,id,'print_acknowledged',actor=current.id,body={'method':'operator','copies':row['copies']})
        return {'state':'completed' if finish else 'printed','control_scan':not finish,'copies':row['copies']}
    @app.post('/api/reprints/{id}/scans')
    def scan(id:UUID,body:Scan,current=dep):
        row,code,item=reprint_access(current,id);current.require('labels.print.confirm',**context(current,item));c=current.connection
        try:parsed=gs1.parse(body.raw.encode('ascii'),scanner=True)
        except (ValueError,UnicodeError):raise HTTPException(422,'Отсканируйте полный КМ с криптохвостом и количеством') from None
        hash=sha256(parsed.normalized).hexdigest()
        prior=c.execute(text('SELECT * FROM reprint_scans WHERE request_id=:id AND request_key=:key'),{'id':id,'key':body.request_id}).mappings().first()
        if prior:
            if prior['payload_sha256']!=hash:raise HTTPException(409,'Идентификатор скана уже использован')
            return {'state':row['state'],'scanned_copies':row['scanned_copies'],'copies':row['copies']}
        current_payload(row,code,item)
        if row['state']!='printed' or not row['policy']['values']['labels.reprint.control_scan']:raise HTTPException(409,'Сначала подтвердите печать')
        if hash!=row['payload_sha256']:raise HTTPException(409,'Скан не совпадает с сохранённой версией этикетки')
        ordinal=row['scanned_copies']+1;done=ordinal==row['copies']
        c.execute(text('INSERT INTO reprint_scans(request_id,request_key,ordinal,payload_sha256,actor_id) VALUES (:id,:key,:ordinal,:sha,:actor)'),{'id':id,'key':body.request_id,'ordinal':ordinal,'sha':hash,'actor':current.id})
        c.execute(text('UPDATE reprint_requests SET scanned_copies=:n,state=:state,completed_at=:time WHERE id=:id'),{'id':id,'n':ordinal,'state':'completed' if done else 'printed','time':datetime.now(timezone.utc) if done else None})
        event(c,id,'reprint_control_scan',actor=current.id,body={'ordinal':ordinal,'completed':done})
        return {'state':'completed' if done else 'printed','scanned_copies':ordinal,'copies':row['copies']}
    @app.post('/api/reprints/{id}/cancel')
    def cancel(id:UUID,body:Resolve,current=dep):
        row,_,_=reprint_access(current,id);c=current.connection
        if row['state']!='prepared':raise HTTPException(409,'Уже напечатанные копии сначала требуют контроля')
        job=c.execute(text('SELECT * FROM print_jobs WHERE reprint_id=:id FOR UPDATE'),{'id':id}).mappings().first()
        if job and job['state'] not in ('queued','held','cancelled'):raise HTTPException(409,'Агент уже получил задание. Сначала сверка результата печати.')
        if job:c.execute(text("UPDATE print_jobs SET state='cancelled' WHERE id=:id"),{'id':job['id']})
        c.execute(text("UPDATE reprint_requests SET state='cancelled' WHERE id=:id"),{'id':id});event(c,id,'reprint_cancelled',actor=current.id,body={'reason':body.reason});return {'state':'cancelled'}
    @app.get('/api/printers')
    def printers(organization_id:UUID,current=dep):
        current.require('labels.print.view',organization_id=organization_id,owner_id=current.id,assigned=True)
        return {'items':[dict(r) for r in current.connection.execute(text('SELECT id,name,printer_name,last_seen_at FROM printer_devices WHERE organization_id=:org AND enabled ORDER BY name,id'),{'org':organization_id}).mappings()]}
    @app.post('/api/business-files/{id}/print')
    def send(id:UUID,body:Send,current=dep):
        c=current.connection;fingerprint=digest({'artifact':id,**body.model_dump()})
        prior=c.execute(text('SELECT * FROM print_jobs WHERE id=:id'),{'id':body.request_id}).mappings().first()
        if prior:
            if prior['actor_id']!=current.id or prior['request_sha256']!=fingerprint:raise HTTPException(409,'Идентификатор запроса уже использован')
            authorize_job(c,prior) if prior['state']=='queued' else current.require('labels.print.view',**context(current,s.item(c,c.execute(text('SELECT item_id FROM business_artifacts WHERE id=:id'),{'id':id}).scalar_one())))
            return public(dict(prior))
        file=c.execute(text('SELECT * FROM business_artifacts WHERE id=:id'),{'id':id}).mappings().first()
        if not file:raise HTTPException(404,'PDF не найден')
        item=s.item(c,file['item_id']);copies=1
        if body.reprint_id:
            request,code,item=reprint_access(current,body.reprint_id);current_payload(request,code,item)
            if request['state']!='prepared' or request['artifact_id']!=id:raise HTTPException(409,'Нужен PDF из текущего задания перепечати')
            if c.execute(text('SELECT 1 FROM print_jobs WHERE reprint_id=:id'),{'id':body.reprint_id}).scalar():raise HTTPException(409,'Задание уже передано агенту; проверьте его результат')
            copies=request['copies']
        elif file['kind']!='labels_pdf':raise HTTPException(422,'Для повторной этикетки сначала создайте задание перепечати')
        if not body.reprint_id and c.execute(text("SELECT EXISTS(SELECT 1 FROM print_jobs WHERE artifact_id=:id AND state<>'cancelled')"),{'id':id}).scalar_one():raise HTTPException(409,'Комплект уже отправлен на печать. Для замены отдельных этикеток используйте перепечать.')
        device=c.execute(text('SELECT * FROM printer_devices WHERE id=:id AND enabled'),{'id':body.printer_id}).mappings().first()
        if not device or device['organization_id']!=item['organization_id']:raise HTTPException(403,'Принтер другой организации или отключён')
        job={'actor_id':current.id,'artifact_id':id,'reprint_id':body.reprint_id};authorize_job(c,job)
        current.require('labels.print.confirm',**context(current,item))
        c.execute(text('''INSERT INTO print_jobs(id,organization_id,actor_id,device_id,artifact_id,reprint_id,copies,request_sha256)
            VALUES (:id,:org,:actor,:device,:file,:reprint,:copies,:sha)'''),{'id':body.request_id,'org':item['organization_id'],'actor':current.id,'device':body.printer_id,'file':id,'reprint':body.reprint_id,'copies':copies,'sha':fingerprint})
        event(c,body.request_id,'print_queued',actor=current.id,body={'copies':copies,'device_id':str(body.printer_id)});return {'id':body.request_id,'state':'queued'}
    @app.get('/api/print-jobs')
    def queue(organization_id:UUID,current=dep):
        if organization_id not in current.organizations():raise HTTPException(403,'Нет доступа к организации')
        rows=current.connection.execute(text('''SELECT j.*,a.item_id,d.name AS printer_name,u.display_name AS actor_name
            FROM print_jobs j JOIN business_artifacts a ON a.id=j.artifact_id JOIN printer_devices d ON d.id=j.device_id JOIN users u ON u.id=j.actor_id
            WHERE j.organization_id=:org ORDER BY CASE j.state WHEN 'unknown' THEN 0 WHEN 'claimed' THEN 1 WHEN 'held' THEN 2 WHEN 'queued' THEN 3 ELSE 4 END,j.created_at DESC,j.id DESC LIMIT 100'''),{'org':organization_id}).mappings().all()
        out=[]
        for row in rows:
            item=s.item(current.connection,row['item_id'])
            try:current.require('labels.print.view',**context(current,item))
            except HTTPException:continue
            out.append({**public(dict(row)),'title':item['title']})
        return {'items':out}
    @app.get('/api/print-jobs/{id}')
    def job(id:UUID,current=dep):
        row=current.connection.execute(text('SELECT * FROM print_jobs WHERE id=:id'),{'id':id}).mappings().first()
        if not row:raise HTTPException(404,'Задание не найдено')
        artifact=current.connection.execute(text('SELECT item_id FROM business_artifacts WHERE id=:id'),{'id':row['artifact_id']}).scalar_one()
        current.require('labels.print.view',**context(current,s.item(current.connection,artifact)))
        return public(dict(row))
    @app.post('/api/print-jobs/{id}/resume')
    def resume(id:UUID,current=dep):
        c=current.connection;row=c.execute(text('SELECT * FROM print_jobs WHERE id=:id FOR UPDATE'),{'id':id}).mappings().first()
        if not row:raise HTTPException(404,'Задание не найдено')
        item_id=c.execute(text('SELECT item_id FROM business_artifacts WHERE id=:id'),{'id':row['artifact_id']}).scalar_one()
        current.require('labels.print.confirm',**context(current,s.item(c,item_id)))
        if row['state'] in ('queued','claimed','spooled'):return {'state':row['state']}
        if row['state']!='held':raise HTTPException(409,'Возобновлять можно только ещё не выданное задание на карантине')
        authorize_job(c,row)
        c.execute(text("UPDATE print_jobs SET state='queued' WHERE id=:id"),{'id':id})
        event(c,id,'print_quarantine_released',actor=current.id);return {'state':'queued'}
    @app.post('/api/print-jobs/{id}/resolve')
    def resolve(id:UUID,body:Resolve,current=dep):
        c=current.connection;row=c.execute(text("SELECT *,claimed_at<now()-interval '5 minutes' AS stale FROM print_jobs WHERE id=:id FOR UPDATE"),{'id':id}).mappings().first()
        if not row:raise HTTPException(404,'Задание не найдено')
        current.require('integrations.manage',organization_id=row['organization_id'],administrative=True)
        if len(body.reason.strip())<8:raise HTTPException(422,'Укажите содержательную причину сверки')
        if row['state'] in ('queued','held') and body.outcome!='cancelled':raise HTTPException(409,'Ещё не выданное задание можно только отменить')
        if row['state']=='claimed' and not row['stale']:raise HTTPException(409,'Агент ещё выполняет печать. Подождите завершения или истечения пяти минут.')
        if row['state'] not in ('unknown','claimed','queued','held'):raise HTTPException(409,'Задание не требует сверки')
        c.execute(text('UPDATE print_jobs SET state=:state,finished_at=now(),receipt=CAST(:receipt AS jsonb) WHERE id=:id'),{'id':id,'state':body.outcome,'receipt':json.dumps({'operator_resolution':body.reason})})
        event(c,id,'print_resolved',actor=current.id,body=body.model_dump());return {'state':body.outcome}
    @app.post('/api/print-agent/claim')
    def claim(request:Request):
        with engine.begin() as c:
            c.execute(text('SELECT pg_advisory_xact_lock_shared(683273529)'));device=device_auth(c,request.headers.get('Authorization','').removeprefix('Bearer '))
            # A claim is never reissued, even after process crash or timeout.
            c.execute(text("UPDATE print_jobs SET state='unknown' WHERE device_id=:id AND state='claimed' AND claimed_at<now()-interval '5 minutes'"),{'id':device['id']})
            if c.execute(text("SELECT 1 FROM print_jobs WHERE device_id=:id AND state IN ('claimed','unknown') LIMIT 1"),{'id':device['id']}).scalar():return {'job':None,'blocked':True}
            row=c.execute(text("SELECT * FROM print_jobs WHERE device_id=:id AND state='queued' ORDER BY created_at,id FOR UPDATE SKIP LOCKED LIMIT 1"),{'id':device['id']}).mappings().first()
            if not row:return {'job':None}
            try:file=authorize_job(c,row)
            except HTTPException as error:
                if isinstance(error.detail,dict) and error.detail.get('code')=='local_quarantine':
                    c.execute(text("UPDATE print_jobs SET state='held' WHERE id=:id"),{'id':row['id']})
                    event(c,row['id'],'print_quarantined',device=device['id']);return {'job':None}
                c.execute(text("UPDATE print_jobs SET state='cancelled' WHERE id=:id"),{'id':row['id']});event(c,row['id'],'print_authorization_revoked',device=device['id']);return {'job':None}
            lease=secrets.token_urlsafe(32)
            c.execute(text("UPDATE print_jobs SET state='claimed',lease_hash=:hash,claimed_at=now() WHERE id=:id"),{'id':row['id'],'hash':sha256(lease.encode()).hexdigest()})
            event(c,row['id'],'print_claimed',device=device['id'])
            return {'job':{'id':row['id'],'lease':lease,'printer_name':device['printer_name'],'copies':row['copies'],'pdf_base64':base64.b64encode(bytes(file['body'])).decode(),'pdf_sha256':file['sha256']}}
    @app.post('/api/print-agent/result')
    def result(body:PrintReceipt,request:Request):
        with engine.begin() as c:
            device=device_auth(c,request.headers.get('Authorization','').removeprefix('Bearer '));row=c.execute(text('SELECT j.*,a.sha256 FROM print_jobs j JOIN business_artifacts a ON a.id=j.artifact_id WHERE j.id=:id FOR UPDATE OF j'),{'id':body.job_id}).mappings().first()
            if not row or row['device_id']!=device['id'] or not secrets.compare_digest(row['lease_hash'] or '',sha256(body.lease.encode()).hexdigest()) or body.pdf_sha256!=row['sha256']:raise HTTPException(403,'Задание, PDF или lease не совпадают')
            if row['receipt']:
                if row['receipt']!=body.model_dump(mode='json',exclude={'lease'}):raise HTTPException(409,'Результат уже сохранён')
                return {'saved':True,'state':row['state']}
            if row['state'] not in ('claimed','unknown'):raise HTTPException(409,'Результат уже разобран оператором')
            receipt=body.model_dump(mode='json',exclude={'lease'})
            c.execute(text('UPDATE print_jobs SET state=:state,finished_at=now(),receipt=CAST(:receipt AS jsonb) WHERE id=:id'),{'id':body.job_id,'state':body.outcome,'receipt':json.dumps(receipt)})
            event(c,body.job_id,'print_result',device=device['id'],body={'outcome':body.outcome,'spool_reference':body.spool_reference})
            return {'saved':True,'state':body.outcome}

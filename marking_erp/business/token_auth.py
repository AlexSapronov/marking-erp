"""True API v747 §§1.5,11.1: signed challenge, encrypted tokens, no manual copy."""
import base64,json,secrets
from datetime import datetime,timedelta,timezone
from hashlib import sha256
from uuid import UUID,uuid4
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.serialization import pkcs7
from cryptography.hazmat.primitives import hashes
from fastapi import Depends,HTTPException,Request
from pydantic import Field,StrictBool
from sqlalchemy import text
import httpx
from .. import auth,signer
from ..auth_routes import Body
from .connections import load
from .errors import RemoteError

BASE='https://markirovka.crpt.ru/api/v3/true-api'
class Refresh(Body):
    organization_id:UUID
class Signed(Body):
    job_id:UUID
    lease:str=Field(min_length=20,max_length=128)
    payload_sha256:str=Field(pattern='^[a-f0-9]{64}$')
    certificate_sha256:str=Field(pattern='^[a-f0-9]{64}$')
    signature_base64:str=Field(min_length=1,max_length=1398104)
    cades_valid:StrictBool
    chain_valid:StrictBool


def call(method,path,body=None):
    try:
        with httpx.Client(timeout=httpx.Timeout(40,connect=8),follow_redirects=False,trust_env=False) as client:
            response=client.request(method,BASE+path,json=body,headers={'Accept':'application/json'})
        if response.status_code!=200:raise RemoteError('Авторизация ЧЗ/СУЗ: HTTP '+str(response.status_code))
        data=response.json()
        if not isinstance(data,dict) or data.get('error_message') or data.get('code'):raise ValueError
        return data
    except (httpx.HTTPError,ValueError):raise RemoteError('Не удалось подтвердить авторизацию ЧЗ/СУЗ') from None

def delegate(c,org,actor_id):
    user=c.execute(text('SELECT * FROM users WHERE id=:id AND enabled'),{'id':actor_id}).mappings().first()
    if not user:raise HTTPException(403,'Администратор, разрешивший обновление, отключён')
    current=auth.Actor(c,dict(user),'renewal',datetime.now(timezone.utc))
    current.require('integrations.manage',organization_id=org,administrative=True)
    return current

def create_jobs(c,settings,org,actor_id):
    delegate(c,org,actor_id);config=load(c,settings,org)
    if config.get('environment')!='production':raise HTTPException(409,'Автоавторизация доступна для подтверждённого промышленного контура')
    revision=c.execute(text('SELECT revision FROM integration_settings WHERE organization_id=:org'),{'org':org}).scalar_one();now=datetime.now(timezone.utc);ids=[]
    c.execute(text("UPDATE integration_auth_jobs SET state='expired',finished_at=now() WHERE organization_id=:org AND state IN ('pending','leased') AND expires_at<now()"),{'org':org})
    for service in ('true','oms'):
        if service=='oms':
            try:UUID(config.get('oms_connection',''))
            except ValueError:
                c.execute(text("INSERT INTO integration_auth_state(organization_id,service,next_refresh_at,error) VALUES (:org,'oms',now()+interval '5 minutes','Укажите корректный OMS Connection UUID в подключениях') ON CONFLICT(organization_id,service) DO UPDATE SET next_refresh_at=EXCLUDED.next_refresh_at,error=EXCLUDED.error"),{'org':org})
                continue
        pending=c.execute(text("SELECT id FROM integration_auth_jobs WHERE organization_id=:org AND service=:service AND state IN ('pending','leased')"),{'org':org,'service':service}).scalar_one_or_none()
        if pending:ids.append(str(pending));continue
        due=c.execute(text('SELECT next_refresh_at<=now() FROM integration_auth_state WHERE organization_id=:org AND service=:service'),{'org':org,'service':service}).scalar_one_or_none()
        if due is False:continue
        response=call('GET','/auth/key')
        try:challenge_uuid=UUID(response['uuid']);payload=response['data'].encode('utf-8')
        except (KeyError,ValueError,AttributeError):raise RemoteError('ЧЗ не вернул корректный запрос подписи') from None
        if not 1<=len(payload)<=8192:raise RemoteError('ЧЗ вернул недопустимый запрос подписи')
        id=uuid4();c.execute(text('''INSERT INTO integration_auth_jobs(id,organization_id,actor_id,service,challenge_uuid,challenge,payload_sha256,connection_revision,oms_connection,expires_at)
            VALUES (:id,:org,:actor,:service,:uuid,:payload,:hash,:revision,:connection,:expires)'''),{'id':id,'org':org,'actor':actor_id,'service':service,'uuid':challenge_uuid,'payload':payload,'hash':sha256(payload).hexdigest(),'revision':revision,'connection':config.get('oms_connection',''),'expires':now+timedelta(minutes=5)})
        c.execute(text("INSERT INTO integration_auth_state(organization_id,service,next_refresh_at,error) VALUES (:org,:service,now()+interval '5 minutes','Ожидается Windows-подписант') ON CONFLICT(organization_id,service) DO UPDATE SET next_refresh_at=EXCLUDED.next_refresh_at,error=EXCLUDED.error"),{'org':org,'service':service})
        auth.audit(c,actor_id,'integration.auth_requested',id,{'service':service});ids.append(str(id))
    return ids

def refresh_tick(engine,settings):
    with engine.begin() as c:
        if not c.execute(text('SELECT pg_try_advisory_xact_lock(683273549)')).scalar_one():return
        c.execute(text('SELECT pg_advisory_xact_lock_shared(683273529)'))
        orgs=c.execute(text("SELECT organization_id,public->>'token_refresh_actor' AS actor FROM integration_settings i WHERE public->>'auto_refresh_tokens'='true' AND (NOT EXISTS(SELECT 1 FROM integration_auth_state a WHERE a.organization_id=i.organization_id) OR EXISTS(SELECT 1 FROM integration_auth_state a WHERE a.organization_id=i.organization_id AND a.next_refresh_at<=now())) ORDER BY updated_at LIMIT 1")).mappings().all()
        for row in orgs:
            try:
                with c.begin_nested():create_jobs(c,settings,row['organization_id'],UUID(row['actor']))
            except Exception:
                for service in ('true','oms'):
                    c.execute(text("INSERT INTO integration_auth_state(organization_id,service,next_refresh_at,error) VALUES (:org,:service,now()+interval '5 minutes','Не удалось обновить доступ. Проверьте подключения, полномочия и подписант.') ON CONFLICT(organization_id,service) DO UPDATE SET next_refresh_at=EXCLUDED.next_refresh_at,error=EXCLUDED.error"),{'org':row['organization_id'],'service':service})

def trusted(c,request):
    c.execute(text('SELECT pg_advisory_xact_lock(683273529)'))
    device=signer.authenticate(c,request.headers.get('Authorization','').removeprefix('Bearer '))
    if not c.execute(text('SELECT enabled FROM signer_verifiers WHERE device_id=:id'),{'id':device['id']}).scalar_one_or_none():raise HTTPException(403,'Требуется зарегистрированный доверенный Windows-верификатор')
    return device

def install_token_auth(app,actor,engine,settings):
    dep=Depends(actor,scope='function')
    @app.get('/api/connections/auth-status')
    def status(organization_id:UUID,current=dep):
        current.require('integrations.manage',organization_id=organization_id,administrative=True)
        rows=current.connection.execute(text('SELECT service,renewed_at,next_refresh_at,error FROM integration_auth_state WHERE organization_id=:org ORDER BY service'),{'org':organization_id}).mappings().all()
        return {'items':[dict(row) for row in rows]}
    @app.post('/api/connections/refresh-tokens')
    def refresh(body:Refresh,current=dep):
        current.require('integrations.manage',organization_id=body.organization_id,administrative=True)
        c=current.connection;c.execute(text('SELECT revision FROM integration_settings WHERE organization_id=:org FOR UPDATE'),{'org':body.organization_id})
        c.execute(text('UPDATE integration_auth_state SET next_refresh_at=now() WHERE organization_id=:org'),{'org':body.organization_id})
        return {'jobs':create_jobs(c,settings,body.organization_id,current.id)}
    @app.post('/api/integration-auth/claim')
    def claim(request:Request):
        with engine.begin() as c:
            device=trusted(c,request)
            c.execute(text("UPDATE integration_auth_jobs SET state='expired',finished_at=now() WHERE state IN ('pending','leased') AND expires_at<now()"))
            job=c.execute(text("SELECT * FROM integration_auth_jobs WHERE organization_id=:org AND state='pending' AND ('auth.'||service)=ANY(CAST(:kinds AS text[])) ORDER BY created_at,id FOR UPDATE SKIP LOCKED LIMIT 1"),{'org':device['organization_id'],'kinds':device['allowed_kinds']}).mappings().first()
            if not job:return {'job':None}
            delegate(c,job['organization_id'],job['actor_id']);lease=secrets.token_urlsafe(32)
            c.execute(text("UPDATE integration_auth_jobs SET state='leased',device_id=:device,lease_hash=:hash WHERE id=:id"),{'id':job['id'],'device':device['id'],'hash':sha256(lease.encode()).hexdigest()})
            return {'job':{'id':job['id'],'lease':lease,'service':job['service'],'payload_base64':base64.b64encode(bytes(job['challenge'])).decode(),'payload_sha256':job['payload_sha256'],'certificate_sha256':device['certificate_sha256'],'signature_format':'cms_attached_der'}}
    @app.post('/api/integration-auth/result')
    def signed(body:Signed,request:Request):
        with engine.begin() as c:
            device=trusted(c,request);job=c.execute(text('SELECT *,expires_at>now() AS live FROM integration_auth_jobs WHERE id=:id FOR UPDATE'),{'id':body.job_id}).mappings().first()
            if not job or job['device_id']!=device['id'] or job['organization_id']!=device['organization_id'] or 'auth.'+job['service'] not in device['allowed_kinds'] or not secrets.compare_digest(job['lease_hash'] or '',sha256(body.lease.encode()).hexdigest()) or body.payload_sha256!=job['payload_sha256'] or body.certificate_sha256!=device['certificate_sha256']:raise HTTPException(403,'Подтверждение не соответствует заданию')
            if job['state']=='confirmed':return {'saved':True}
            if job['state']!='leased' or not job['live']:raise HTTPException(409,'Авторизационный запрос истёк; получите новый')
            delegate(c,job['organization_id'],job['actor_id'])
            if not body.cades_valid or not body.chain_valid:raise HTTPException(422,'Не подтверждена подпись и цепочка сертификата')
            try:
                signature=base64.b64decode(body.signature_base64,validate=True);certs=pkcs7.load_der_pkcs7_certificates(signature)
                if not 1<=len(signature)<=1048576 or not any(cert.fingerprint(hashes.SHA256()).hex()==device['certificate_sha256'] for cert in certs):raise ValueError
            except (ValueError,TypeError):raise HTTPException(422,'Некорректная CMS или сертификат') from None
            row=c.execute(text('SELECT * FROM integration_settings WHERE organization_id=:org FOR UPDATE'),{'org':job['organization_id']}).mappings().one();config=load(c,settings,job['organization_id'])
            if config['environment']!='production' or (job['service']=='oms' and config.get('oms_connection')!=job['oms_connection']):raise HTTPException(409,'Настройки подключения изменились')
            inn=c.execute(text('SELECT inn FROM organizations WHERE id=:org'),{'org':job['organization_id']}).scalar_one()
            path='/auth/simpleSignIn'+('/'+str(UUID(job['oms_connection'])) if job['service']=='oms' else '')
            payload={'uuid':str(job['challenge_uuid']),'data':body.signature_base64,'inn':inn}
            if job['service']=='true':payload['unitedToken']=True
            # Auth exchanges are replayable session establishment, never a paid/legal mutation.
            result=call('POST',path,payload);key='true_token' if job['service']=='true' else 'oms_token'
            try:token=str(UUID(result['uuidToken' if job['service']=='true' else 'token']))
            except (KeyError,TypeError,ValueError):raise HTTPException(409,'Сервис не вернул действующий UUID-токен') from None
            now=datetime.now(timezone.utc);renew=now+timedelta(hours=8)
            if job['service']=='true' and result.get('expireDate'):
                try:expiry=datetime.fromisoformat(result['expireDate'].replace('Z','+00:00'));renew=min(renew,expiry-timedelta(minutes=30))
                except (ValueError,TypeError):raise HTTPException(409,'ЧЗ вернул некорректный срок токена') from None
                if expiry<=now:raise HTTPException(409,'ЧЗ вернул истёкший токен')
            fernet=Fernet(settings.mfa_encryption_key.get_secret_value().encode());secret=json.loads(fernet.decrypt(bytes(row['secret'])));secret[key]=token
            c.execute(text('UPDATE integration_settings SET secret=:secret,revision=revision+1,updated_at=now() WHERE organization_id=:org'),{'org':job['organization_id'],'secret':fernet.encrypt(json.dumps(secret).encode())})
            c.execute(text("UPDATE integration_auth_jobs SET state='confirmed',finished_at=now() WHERE id=:id"),{'id':job['id']})
            c.execute(text("UPDATE integration_auth_state SET renewed_at=now(),next_refresh_at=:refresh,error='' WHERE organization_id=:org AND service=:service"),{'org':job['organization_id'],'service':job['service'],'refresh':max(renew,now+timedelta(minutes=1))})
            auth.audit(c,job['actor_id'],'integration.token_renewed',job['id'],{'service':job['service'],'device_id':str(device['id'])});return {'saved':True}

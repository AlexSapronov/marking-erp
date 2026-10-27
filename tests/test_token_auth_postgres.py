"""Delegated token renewal protocol; synthetic signatures and HTTP fixtures only."""
import base64,json
from datetime import datetime,timedelta,timezone
from uuid import uuid4
import pytest
from sqlalchemy import text
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs7
from test_auth_postgres import env,client,login
from test_business_postgres import connections
from marking_erp import signer
from marking_erp.business import token_auth
from marking_erp.business.connections import load
pytestmark=[pytest.mark.postgres,pytest.mark.usefixtures('fresh_schema')]

@pytest.fixture
def auth_device(env,engine):
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048);name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'Synthetic token renewal test')]);now=datetime.now(timezone.utc)
    cert=x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(days=1)).not_valid_after(now+timedelta(days=1)).sign(key,hashes.SHA256())
    pin=cert.fingerprint(hashes.SHA256()).hex()
    def cms(data):return base64.b64encode(pkcs7.PKCS7SignatureBuilder().set_data(data).add_signer(cert,key,hashes.SHA256()).sign(serialization.Encoding.DER,[pkcs7.PKCS7Options.Binary])).decode()
    with client(env) as c:login(c,'administrator');connections(c,env,oms_connection=str(uuid4()),auto_refresh_tokens=True)
    with engine.begin() as c:
        id,token=signer.provision(c,env[1],'Synthetic auth agent',pin,['auth.true','auth.oms'])
        c.execute(text('INSERT INTO signer_verifiers(device_id) VALUES (:id)'),{'id':id})
    return id,token,pin,cms

def test_signed_renewal_saves_both_tokens_encrypted_without_exposing_them(env,engine,auth_device,monkeypatch):
    id,token,pin,cms=auth_device;fresh_true=str(uuid4());fresh_oms=str(uuid4());calls=[]
    def remote(method,path,body=None):
        calls.append((method,path,body))
        if method=='GET':return {'uuid':str(uuid4()),'data':'exact-random-challenge-'+str(uuid4())}
        assert body['uuid'] and body['inn'] and body['data']
        if path=='/auth/simpleSignIn':
            assert body['unitedToken'] is True
            return {'uuidToken':fresh_true,'expireDate':(datetime.now(timezone.utc)+timedelta(hours=10)).isoformat()}
        return {'token':fresh_oms}
    monkeypatch.setattr(token_auth,'call',remote)
    token_auth.refresh_tick(engine,env[0])
    with client(env) as c:
        login(c,'specialist');assert c.post('/api/connections/refresh-tokens',json={'organization_id':str(env[1])}).status_code==403
        assert c.post('/api/integration-auth/claim').status_code==401
    with client(env) as agent:
        agent.headers['Authorization']='Bearer '+token
        for _ in range(2):
            job=agent.post('/api/integration-auth/claim').json()['job'];assert job['signature_format']=='cms_attached_der'
            body={'job_id':job['id'],'lease':job['lease'],'payload_sha256':job['payload_sha256'],'certificate_sha256':pin,'signature_base64':cms(base64.b64decode(job['payload_base64'])),'cades_valid':True,'chain_valid':True}
            response=agent.post('/api/integration-auth/result',json=body);assert response.status_code==200,response.text
            assert fresh_true not in response.text and fresh_oms not in response.text
            assert agent.post('/api/integration-auth/result',json=body).json()['saved']
        assert agent.post('/api/integration-auth/claim').json()['job'] is None
    with engine.connect() as c:
        config=load(c,env[0],env[1]);assert config['true_token']==fresh_true and config['oms_token']==fresh_oms
        ciphertext=c.execute(text('SELECT secret FROM integration_settings')).scalar_one();assert fresh_true.encode() not in bytes(ciphertext)
        events=c.execute(text('SELECT detail FROM audit_events')).scalars().all();assert fresh_true not in json.dumps(events,default=str)
        assert c.execute(text("SELECT count(*) FROM integration_auth_jobs WHERE state='confirmed'")).scalar_one()==2
    token_auth.refresh_tick(engine,env[0]);assert len(calls)==4

def test_revoked_auth_signer_cannot_claim_or_update(env,engine,auth_device,monkeypatch):
    id,token,pin,cms=auth_device
    monkeypatch.setattr(token_auth,'call',lambda *a,**kw:{'uuid':str(uuid4()),'data':'challenge'})
    token_auth.refresh_tick(engine,env[0])
    with engine.begin() as c:c.execute(text('UPDATE signer_devices SET enabled=false WHERE id=:id'),{'id':id})
    with client(env) as agent:
        agent.headers['Authorization']='Bearer '+token
        assert agent.post('/api/integration-auth/claim').status_code==401

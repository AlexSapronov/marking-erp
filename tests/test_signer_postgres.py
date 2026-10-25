import base64
from datetime import datetime,timedelta,timezone
from pathlib import Path
import subprocess
import tempfile
import pytest
from sqlalchemy import text
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs7
from test_auth_postgres import env,client,login
from test_operations_postgres import queue_env,prepare,state,PAYLOAD
from marking_erp import signer,operations as ops

pytestmark=[pytest.mark.postgres,pytest.mark.usefixtures('fresh_schema')]

@pytest.fixture(scope='module')
def certificate():
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048);name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'Synthetic protocol test')]);now=datetime.now(timezone.utc)
    cert=x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(days=1)).not_valid_after(now+timedelta(days=1)).sign(key,hashes.SHA256())
    def cms(data):return pkcs7.PKCS7SignatureBuilder().set_data(data).add_signer(cert,key,hashes.SHA256()).sign(serialization.Encoding.DER,[pkcs7.PKCS7Options.DetachedSignature,pkcs7.PKCS7Options.Binary])
    def verify(data,signature,pin):
        if pin!=cert.fingerprint(hashes.SHA256()).hex():return False
        with tempfile.TemporaryDirectory() as root:
            p=Path(root);(p/'data').write_bytes(data);(p/'cms').write_bytes(signature);(p/'cert.pem').write_bytes(cert.public_bytes(serialization.Encoding.PEM))
            result=subprocess.run(['openssl','cms','-verify','-binary','-inform','DER','-in',str(p/'cms'),'-content',str(p/'data'),'-noverify','-nointern','-certfile',str(p/'cert.pem'),'-out',str(p/'output')],capture_output=True,timeout=10)
            return result.returncode==0 and (p/'output').read_bytes()==data
    return cert.fingerprint(hashes.SHA256()).hex(),cms,verify

@pytest.fixture
def signer_env(queue_env,engine,certificate):
    with engine.begin() as c:
        op=prepare(c,queue_env,'fixture.signed');id,token=signer.provision(c,queue_env[0][1],'Fixture device',certificate[0],['fixture.signed'])
    return queue_env,op,id,token


def claim(c,token):return c.post('/api/signer/claim',headers={'Authorization':'Bearer '+token}).json()['job']

def result(job,signature):return {k:job[k] for k in ('job_id','fence','lease','payload_sha256','certificate_sha256')}|{'signature_base64':base64.b64encode(signature).decode()}


def test_separate_machine_identity_and_user_cookie_cannot_claim(signer_env):
    queue,op,id,token=signer_env
    with client(queue[0]) as c:
        assert c.post('/api/signer/claim').status_code==401
        login(c,'administrator');assert c.post('/api/signer/claim').status_code==401
        assert c.post('/api/signer/claim',headers={'Authorization':'Bearer wrong'}).status_code==401
        c.headers.pop('Origin');c.headers.pop('X-Requested-With');c.headers.pop('X-CSRF-Token')
        job=claim(c,token);assert base64.b64decode(job['payload_base64'])==PAYLOAD
        assert claim(c,token) is None


def test_receipt_is_not_verification_real_cms_byte_binding(signer_env,engine,certificate):
    queue,op,id,token=signer_env
    with client(queue[0]) as c:
        job=claim(c,token);response=c.post('/api/signer/result',json=result(job,certificate[1](PAYLOAD)),headers={'Authorization':'Bearer '+token})
        assert response.status_code==200 and response.json()=={'accepted':True,'verified':False,'state':'received_unverified'}
    with engine.begin() as c:
        assert signer.verify_received(c,job['job_id']) is False and state(c,op['id'])=='awaiting_signature'
        assert ops.claim(c,'worker') is None
        assert signer.verify_received(c,job['job_id'],verifier=certificate[2],verification_ref='synthetic RSA/OpenSSL test; not GOST trust') is True
        ticket=ops.claim(c,'worker');assert ticket['id']==op['id'] and ticket['signature']


def test_wrong_signed_bytes_and_wrong_hash_never_dispatch(signer_env,engine,certificate):
    queue,op,id,token=signer_env
    with client(queue[0]) as c:
        job=claim(c,token);body=result(job,certificate[1](PAYLOAD+b'changed'));body['payload_sha256']='0'*64
        assert c.post('/api/signer/result',json=body,headers={'Authorization':'Bearer '+token}).status_code==403
        body['payload_sha256']=job['payload_sha256'];assert c.post('/api/signer/result',json=body,headers={'Authorization':'Bearer '+token}).json()['accepted'] is True
    with engine.begin() as c:
        assert signer.verify_received(c,job['job_id'],verifier=certificate[2],verification_ref='byte mismatch test') is False
        assert state(c,op['id'])=='blocked' and ops.claim(c,'worker') is None


def test_expired_lease_old_fence_saved_but_never_applied(signer_env,engine,certificate):
    queue,op,id,token=signer_env
    with client(queue[0]) as c:
        first=claim(c,token)
        with engine.begin() as db:db.execute(text("UPDATE signer_jobs SET lease_until=clock_timestamp()-interval '1 second'"))
        second=claim(c,token);assert second['fence']==first['fence']+1
        response=c.post('/api/signer/result',json=result(first,certificate[1](PAYLOAD)),headers={'Authorization':'Bearer '+token});assert response.json()['accepted'] is False
        response=c.post('/api/signer/result',json=result(second,certificate[1](PAYLOAD)),headers={'Authorization':'Bearer '+token});assert response.json()['accepted'] is True
    with engine.connect() as db:
        assert db.execute(text('SELECT count(*) FROM signer_receipts')).scalar_one()==2
        assert state(db,op['id'])=='awaiting_signature'


def test_stopped_target_or_revoked_device_blocks_signing(signer_env,engine):
    queue,op,id,token=signer_env
    with engine.begin() as c:c.execute(text('UPDATE operation_targets SET stopped=true'))
    with client(queue[0]) as c:assert claim(c,token) is None
    with engine.begin() as c:
        assert state(c,op['id'])=='blocked'
        c.execute(text('UPDATE signer_devices SET enabled=false WHERE id=:id'),{'id':id})
    with client(queue[0]) as c:assert c.post('/api/signer/claim',headers={'Authorization':'Bearer '+token}).status_code==401
    with engine.connect() as c:assert c.execute(text('SELECT token_hash FROM signer_devices WHERE id=:id'),{'id':id}).scalar_one()!=token

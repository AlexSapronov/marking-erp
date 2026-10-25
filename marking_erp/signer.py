"""Lease protocol only. CMS receipt is NOT cryptographic verification."""
import base64
from hashlib import sha256
import hmac
import re
import secrets
from uuid import uuid4,UUID
from cryptography.hazmat.primitives.serialization import pkcs7
from cryptography.hazmat.primitives import hashes
from fastapi import HTTPException,Request
from pydantic import Field,StrictInt
from sqlalchemy import text
from .auth_routes import Body
from .operations import authorization,transition


class SignatureResult(Body):
    job_id:UUID
    fence:StrictInt=Field(ge=1)
    lease:str=Field(min_length=20,max_length=128)
    payload_sha256:str=Field(pattern=r'^[a-f0-9]{64}$')
    certificate_sha256:str=Field(pattern=r'^[a-f0-9]{64}$')
    signature_base64:str=Field(min_length=1,max_length=1398104)


def provision(c,organization_id,name,certificate_sha256,allowed_kinds):
    if not name.strip() or not re.fullmatch('[a-f0-9]{64}',certificate_sha256) or not allowed_kinds:raise ValueError('Provide a pinned certificate and explicit kinds')
    if not c.execute(text('SELECT 1 FROM organizations WHERE id=:id AND enabled'),{'id':organization_id}).scalar():raise ValueError('Organization unavailable')
    for kind in allowed_kinds:
        if not c.execute(text('SELECT 1 FROM operation_contracts WHERE kind=:kind AND requires_signature AND approved'),{'kind':kind}).scalar():raise ValueError('No approved signing contract for kind')
    token=secrets.token_urlsafe(48);id=uuid4()
    c.execute(text('''INSERT INTO signer_devices(id,organization_id,name,token_hash,certificate_sha256,allowed_kinds)
        VALUES (:id,:org,:name,:hash,:cert,:kinds)'''),{'id':id,'org':organization_id,'name':name.strip(),'hash':sha256(token.encode()).hexdigest(),'cert':certificate_sha256,'kinds':allowed_kinds})
    return id,token


def authenticate(c,token):
    if not token or len(token)>128:raise HTTPException(401,'Требуется отдельный токен подписанта')
    device=c.execute(text('''SELECT s.* FROM signer_devices s JOIN organizations o ON o.id=s.organization_id
        WHERE s.token_hash=:hash AND s.enabled AND o.enabled FOR UPDATE OF s'''),{'hash':sha256(token.encode()).hexdigest()}).mappings().first()
    if not device:raise HTTPException(401,'Требуется отдельный токен подписанта')
    c.execute(text('UPDATE signer_devices SET last_seen_at=clock_timestamp() WHERE id=:id'),{'id':device['id']})
    return device


def claim_signature(c,device):
    # Device row is locked by authenticate: one active token job per signer.
    if c.execute(text("SELECT 1 FROM signer_jobs WHERE device_id=:device AND state='leased' AND lease_until>clock_timestamp()"),{'device':device['id']}).scalar():return None
    jobs=c.execute(text('''SELECT j.id AS job_id,j.fence AS job_fence,o.* FROM signer_jobs j JOIN external_operations o ON o.id=j.operation_id
        WHERE o.organization_id=:org AND o.kind=ANY(CAST(:kinds AS text[])) AND o.state='awaiting_signature'
        AND (j.state='pending' OR (j.state='leased' AND j.lease_until<clock_timestamp()))
        ORDER BY j.created_at,j.id FOR UPDATE OF j,o SKIP LOCKED LIMIT 1'''),{'org':device['organization_id'],'kinds':device['allowed_kinds']}).mappings().first()
    if not jobs:return None
    reason=authorization(c,jobs)
    if reason:
        c.execute(text("UPDATE signer_jobs SET state='cancelled',lease_hash=NULL,lease_until=NULL WHERE id=:id"),{'id':jobs['job_id']})
        transition(c,jobs['id'],'blocked',reason);return None
    lease=secrets.token_urlsafe(32)
    c.execute(text("UPDATE signer_jobs SET state='leased',fence=fence+1,device_id=:device,certificate_sha256=:cert,lease_hash=:hash,lease_until=clock_timestamp()+interval '60 seconds' WHERE id=:id"),{'id':jobs['job_id'],'device':device['id'],'cert':device['certificate_sha256'],'hash':sha256(lease.encode()).hexdigest()})
    c.execute(text('INSERT INTO signer_leases(job_id,fence,device_id,lease_hash,certificate_sha256,payload_sha256) VALUES (:job,:fence,:device,:hash,:cert,:payload)'),{'job':jobs['job_id'],'fence':jobs['job_fence']+1,'device':device['id'],'hash':sha256(lease.encode()).hexdigest(),'cert':device['certificate_sha256'],'payload':jobs['payload_sha256']})
    return {'job_id':str(jobs['job_id']),'operation_id':str(jobs['id']),'organization_id':str(jobs['organization_id']),'kind':jobs['kind'],'contract_version':jobs['contract_version'],
            'fence':jobs['job_fence']+1,'lease':lease,'lease_seconds':60,'payload_base64':base64.b64encode(bytes(jobs['payload'])).decode(),'payload_sha256':jobs['payload_sha256'],
            'certificate_sha256':device['certificate_sha256'],'signature_format':'cms_detached_der','target_revision':jobs['target_revision']}


def receive(c,device,body):
    prior=c.execute(text('SELECT * FROM signer_leases WHERE job_id=:job AND fence=:fence AND device_id=:device'),{'job':body.job_id,'fence':body.fence,'device':device['id']}).mappings().first()
    if not prior or not hmac.compare_digest(prior['lease_hash'],sha256(body.lease.encode()).hexdigest()) or prior['payload_sha256']!=body.payload_sha256 or prior['certificate_sha256']!=body.certificate_sha256:raise HTTPException(403,'Задание, байты или lease не совпадают')
    try:
        signature=base64.b64decode(body.signature_base64,validate=True)
        if not 1<=len(signature)<=1048576:raise ValueError
        certs=pkcs7.load_der_pkcs7_certificates(signature)
        if not any(cert.fingerprint(hashes.SHA256()).hex()==prior['certificate_sha256'] for cert in certs):raise ValueError
    except (ValueError,TypeError):raise HTTPException(422,'Некорректный CMS или сертификат') from None
    job=c.execute(text('SELECT *,lease_until>clock_timestamp() AS live FROM signer_jobs WHERE id=:id FOR UPDATE'),{'id':body.job_id}).mappings().first()
    if not job:raise HTTPException(404,'Задание не найдено')
    op=c.execute(text('SELECT * FROM external_operations WHERE id=:id FOR UPDATE'),{'id':job['operation_id']}).mappings().one()
    valid=job['device_id']==device['id'] and job['state']=='leased' and job['fence']==body.fence and job['live'] and hmac.compare_digest(job['lease_hash'] or '',sha256(body.lease.encode()).hexdigest())
    valid=bool(valid and body.payload_sha256==op['payload_sha256'] and body.certificate_sha256==job['certificate_sha256'] and job['certificate_sha256']==device['certificate_sha256'] and op['state']=='awaiting_signature' and authorization(c,op) is None)
    c.execute(text('INSERT INTO signer_receipts(job_id,device_id,fence,signature,sha256,applied) VALUES (:job,:device,:fence,:signature,:sha,:valid)'),{'job':job['id'],'device':device['id'],'fence':body.fence,'signature':signature,'sha':sha256(signature).hexdigest(),'valid':valid})
    if valid:c.execute(text("UPDATE signer_jobs SET state='received',signature=:signature,signature_sha256=:sha,lease_hash=NULL,lease_until=NULL WHERE id=:id"),{'signature':signature,'sha':sha256(signature).hexdigest(),'id':job['id']})
    # Even a correctly structured CMS stays unverified and cannot be dispatched.
    return {'accepted':valid,'verified':False,'state':'received_unverified' if valid else 'stale_evidence_saved'}


def verify_received(c,job_id,*,verifier=None,verification_ref=''):
    """Trusted adapter must verify CMS exact bytes, pinned signer, chain/revocation.

    No production verifier is installed. None is fail-closed; certificate presence
    in a received CMS is never treated as a valid cryptographic signature.
    """
    c.execute(text('SELECT pg_advisory_xact_lock(683273529)'))
    job=c.execute(text('SELECT * FROM signer_jobs WHERE id=:id FOR UPDATE'),{'id':job_id}).mappings().one()
    if job['state']!='received':return False
    op=c.execute(text('SELECT * FROM external_operations WHERE id=:id FOR UPDATE'),{'id':job['operation_id']}).mappings().one()
    if verifier is None:return False
    if not verification_ref.strip():raise ValueError('Verified evidence reference is required')
    valid=not authorization(c,op) and op['state']=='awaiting_signature' and sha256(bytes(job['signature'])).hexdigest()==job['signature_sha256']
    if valid:
        try:valid=verifier(bytes(op['payload']),bytes(job['signature']),job['certificate_sha256']) is True
        except Exception:valid=False
    if valid:
        c.execute(text("UPDATE signer_jobs SET state='verified',verification_ref=:ref WHERE id=:id"),{'ref':verification_ref,'id':job_id})
        transition(c,op['id'],'queued','signature_verified')
    else:
        c.execute(text("UPDATE signer_jobs SET state='rejected' WHERE id=:id"),{'id':job_id})
        transition(c,op['id'],'blocked','signature_invalid_or_stale')
    return bool(valid)


def install_signer(app,engine):
    def device(request:Request):
        header=request.headers.get('authorization','')
        token=header[7:] if header.startswith('Bearer ') else ''
        with engine.begin() as c:
            c.execute(text('SELECT pg_advisory_xact_lock(683273529)'))
            yield c,authenticate(c,token)
    from fastapi import Depends
    @app.post('/api/signer/claim')
    def claim_route(context=Depends(device,scope='function')):
        c,current=context
        return {'job':claim_signature(c,current)}
    @app.post('/api/signer/result')
    def result_route(body:SignatureResult,context=Depends(device,scope='function')):
        c,current=context
        return receive(c,current,body)

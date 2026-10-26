"""Separately provisioned trusted Windows CryptoPro verification adapter."""
from hashlib import sha256
from uuid import UUID
from fastapi import HTTPException,Request
from pydantic import Field,StrictInt,StrictBool
from sqlalchemy import text
from ..auth_routes import Body
from .. import signer,auth

class Attestation(Body):
    job_id:UUID
    fence:StrictInt=Field(ge=1)
    payload_sha256:str=Field(pattern='^[a-f0-9]{64}$')
    signature_sha256:str=Field(pattern='^[a-f0-9]{64}$')
    certificate_sha256:str=Field(pattern='^[a-f0-9]{64}$')
    cades_valid:StrictBool
    chain_valid:StrictBool


def install_verifier(app,engine):
    @app.post('/api/signer/verification')
    def verify(body:Attestation,request:Request):
        token=request.headers.get('Authorization','').removeprefix('Bearer ')
        with engine.begin() as c:
            device=signer.authenticate(c,token)
            trusted=c.execute(text('SELECT enabled FROM signer_verifiers WHERE device_id=:id'),{'id':device['id']}).scalar_one_or_none()
            if trusted is not True:raise HTTPException(403,'Windows-верификатор не зарегистрирован администратором сервера')
            job=c.execute(text('SELECT j.*,o.payload_sha256 FROM signer_jobs j JOIN external_operations o ON o.id=j.operation_id WHERE j.id=:id FOR UPDATE OF j,o'),{'id':body.job_id}).mappings().first()
            if not job or job['device_id']!=device['id'] or job['fence']!=body.fence or job['state']!='received':raise HTTPException(409,'Задание изменилось или подпись не получена')
            if (body.payload_sha256!=job['payload_sha256'] or body.signature_sha256!=job['signature_sha256'] or body.certificate_sha256!=device['certificate_sha256']):raise HTTPException(409,'Подтверждение не соответствует подписи, payload или сертификату')
            # The bearer identity is a root-provisioned trusted remote verifier.
            # No employee API or browser can assert cryptographic verification.
            valid=body.cades_valid and body.chain_valid
            ref='windows-cades:'+str(device['id'])+':'+body.signature_sha256
            result=signer.verify_received(c,body.job_id,verifier=lambda payload,signature,cert:bool(valid and sha256(payload).hexdigest()==body.payload_sha256 and sha256(signature).hexdigest()==body.signature_sha256 and cert==body.certificate_sha256),verification_ref=ref)
            auth.audit(c,None,'signer.verification',body.job_id,{'verified':result})
            return {'verified':result}

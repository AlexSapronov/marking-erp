"""Durable commands. No production HTTP adapter is registered by this module."""
from dataclasses import dataclass
from hashlib import sha256
import hmac
import json
import secrets
from uuid import uuid4
from sqlalchemy import text
from fastapi import HTTPException
from .auth import Actor
from .policy import snapshot

MAX_BYTES=1048576

class CommandConflict(ValueError):pass


class RetryAfter(Exception):
    def __init__(self,seconds):
        if type(seconds) is not int or not 1<=seconds<=3600:raise ValueError('Invalid Retry-After')
        self.seconds=seconds
        super().__init__('Remote service requested a delay')


def event(c,id,state,reason=''):
    c.execute(text('INSERT INTO operation_events(operation_id,state,reason) VALUES (:id,:state,:reason)'),{'id':id,'state':state,'reason':reason})


def transition(c,id,state,reason=''):
    c.execute(text('UPDATE external_operations SET state=:state,reason=:reason,updated_at=clock_timestamp() WHERE id=:id'),{'id':id,'state':state,'reason':reason})
    event(c,id,state,reason)


def authorization(c,op):
    user=c.execute(text('SELECT * FROM users WHERE id=:id AND enabled'),{'id':op['actor_id']}).mappings().first()
    target=c.execute(text('SELECT * FROM operation_targets WHERE id=:id'),{'id':op['target_id']}).mappings().first()
    contract=c.execute(text('SELECT * FROM operation_contracts WHERE kind=:kind'),{'kind':op['kind']}).mappings().first()
    if not contract or not contract['approved'] or contract['version']!=op['contract_version'] or contract['permission_id']!=op['permission_id'] or contract['semantic']!=op['semantic'] or contract['requires_signature']!=op['requires_signature']:return 'contract_unavailable'
    if not target or target['stopped']:return 'target_stopped'
    if target['revision']!=op['target_revision'] or target['organization_id']!=op['organization_id']:return 'target_revision_changed'
    if not user:return 'actor_disabled'
    if c.execute(text('SELECT administrative FROM permissions WHERE id=:id'),{'id':op['permission_id']}).scalar():return 'administrative_command_forbidden'
    assigned=c.execute(text('SELECT 1 FROM operation_assignments WHERE target_id=:target AND user_id=:user'),{'target':target['id'],'user':user['id']}).scalar() is not None
    try:Actor(c,dict(user),'background',None).require(op['permission_id'],organization_id=op['organization_id'],owner_id=target['owner_id'],assigned=assigned)
    except HTTPException:return 'permission_revoked'
    if sha256(bytes(op['payload'])).hexdigest()!=op['payload_sha256']:return 'payload_corrupted'
    return None


def prepare(c,*,organization_id,actor_id,kind,target_id,target_revision,idempotency_key,payload):
    if type(payload) is not bytes or not 1<=len(payload)<=MAX_BYTES or not 1<=len(idempotency_key)<=120 or type(target_revision) is not int or target_revision<1:raise ValueError('Invalid command size or key')
    c.execute(text('SELECT pg_advisory_xact_lock(683273529)'))
    c.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:key,683273531))'),{'key':str(organization_id)+kind+idempotency_key})
    contract=c.execute(text('SELECT * FROM operation_contracts WHERE kind=:kind FOR SHARE'),{'kind':kind}).mappings().first()
    if not contract:raise ValueError('No registered contract')
    digest=sha256(payload).hexdigest();intent=sha256(json.dumps([str(organization_id),str(actor_id),kind,str(target_id),target_revision,contract['version'],digest],separators=(',',':')).encode()).hexdigest()
    existing=c.execute(text('SELECT * FROM external_operations WHERE organization_id=:org AND kind=:kind AND idempotency_key=:key'),{'org':organization_id,'kind':kind,'key':idempotency_key}).mappings().first()
    if existing:
        if existing['intent_sha256']!=intent:raise CommandConflict('Idempotency key belongs to a different command')
        return dict(existing)
    if contract['semantic']=='mutation' and c.execute(text("SELECT 1 FROM external_operations WHERE organization_id=:org AND kind=:kind AND target_id=:target AND target_revision=:revision AND semantic='mutation'"),{'org':organization_id,'kind':kind,'target':target_id,'revision':target_revision}).scalar():raise CommandConflict('Source revision already has this mutation; a new key cannot bypass it')
    target=c.execute(text('SELECT * FROM operation_targets WHERE id=:id FOR SHARE'),{'id':target_id}).mappings().first()
    if not target or target['organization_id']!=organization_id:raise ValueError('Unknown target organization')
    id=uuid4();state='awaiting_signature' if contract['requires_signature'] else 'queued'
    op={'id':id,'organization_id':organization_id,'actor_id':actor_id,'kind':kind,'contract_version':contract['version'],'semantic':contract['semantic'],'permission_id':contract['permission_id'],
        'requires_signature':contract['requires_signature'],'target_id':target_id,'target_revision':target_revision,'payload':payload,'payload_sha256':digest}
    reason=authorization(c,op)
    if reason:state='blocked'
    c.execute(text('''INSERT INTO external_operations(id,organization_id,actor_id,kind,contract_version,semantic,permission_id,requires_signature,target_id,target_revision,idempotency_key,intent_sha256,payload,payload_sha256,policy_snapshot,state,reason)
        VALUES (:id,:organization_id,:actor_id,:kind,:contract_version,:semantic,:permission_id,:requires_signature,:target_id,:target_revision,:key,:intent,:payload,:payload_sha256,CAST(:policy AS jsonb),:state,:reason)'''),
        {**op,'key':idempotency_key,'intent':intent,'policy':json.dumps(snapshot(c,organization_id)),'state':state,'reason':reason or ''})
    event(c,id,state,reason or '')
    if state=='awaiting_signature':c.execute(text("INSERT INTO signer_jobs(id,operation_id,state) VALUES (:id,:op,'pending')"),{'id':uuid4(),'op':id})
    return dict(c.execute(text('SELECT * FROM external_operations WHERE id=:id'),{'id':id}).mappings().one())


def recover_expired(c):
    rows=c.execute(text("SELECT * FROM external_operations WHERE state='dispatching' AND lease_until<clock_timestamp() FOR UPDATE SKIP LOCKED")).mappings().all()
    for op in rows:
        state='queued' if op['semantic']=='read' and op['fence']<5 else 'unknown'
        transition(c,op['id'],state,'lease_expired')
        c.execute(text('UPDATE external_operations SET lease_hash=NULL,lease_until=NULL,available_at=clock_timestamp()+interval \'5 seconds\' WHERE id=:id'),{'id':op['id']})
    return len(rows)


def claim(c,worker_id,*,lease_seconds=60):
    if not worker_id or len(worker_id)>100 or not 5<=lease_seconds<=300:raise ValueError('Invalid lease')
    # Same authorization fence as identity/configuration edits; never held during IO.
    c.execute(text('SELECT pg_advisory_xact_lock(683273529)'))
    recover_expired(c)
    op=c.execute(text("SELECT * FROM external_operations WHERE state='queued' AND available_at<=clock_timestamp() ORDER BY available_at,created_at,id FOR UPDATE SKIP LOCKED LIMIT 1")).mappings().first()
    if not op:return None
    reason=authorization(c,op)
    signature=None
    if op['requires_signature']:
        signature=c.execute(text("SELECT j.signature,j.signature_sha256 FROM signer_jobs j JOIN signer_devices d ON d.id=j.device_id WHERE j.operation_id=:id AND j.state='verified' AND d.enabled AND d.organization_id=:org AND d.certificate_sha256=j.certificate_sha256 AND :kind=ANY(d.allowed_kinds)"),{'id':op['id'],'org':op['organization_id'],'kind':op['kind']}).mappings().first()
        if not signature or sha256(bytes(signature['signature'])).hexdigest()!=signature['signature_sha256']:reason='signature_not_verified'
    if reason:transition(c,op['id'],'blocked',reason);return None
    lease=secrets.token_urlsafe(32);attempt=uuid4()
    c.execute(text("UPDATE external_operations SET state='dispatching',fence=fence+1,lease_hash=:lease,lease_until=clock_timestamp()+make_interval(secs=>:seconds),worker_id=:worker,updated_at=clock_timestamp() WHERE id=:id"),{'lease':sha256(lease.encode()).hexdigest(),'seconds':lease_seconds,'worker':worker_id,'id':op['id']})
    c.execute(text('INSERT INTO operation_attempts(id,operation_id,fence,worker_id) VALUES (:id,:op,:fence,:worker)'),{'id':attempt,'op':op['id'],'fence':op['fence']+1,'worker':worker_id})
    event(c,op['id'],'dispatching')
    return {**dict(op),'state':'dispatching','attempt_id':attempt,'fence':op['fence']+1,'lease':lease,'payload':bytes(op['payload']),'signature':bytes(signature['signature']) if signature else None}


@dataclass(frozen=True)
class Result:
    raw:bytes
    classification:str # submitted / confirmed (read only) / rejected / unknown
    payload_sha256:str
    organization_id:str
    external_id:str|None=None


def finish(c,ticket,result):
    if not isinstance(result,Result) or type(result.raw) is not bytes or not 1<=len(result.raw)<=MAX_BYTES:raise ValueError('Evidence is required')
    op=c.execute(text('SELECT *,lease_until>clock_timestamp() AS live FROM external_operations WHERE id=:id FOR UPDATE'),{'id':ticket['id']}).mappings().one()
    valid=op['state']=='dispatching' and op['fence']==ticket['fence'] and op['live'] and hmac.compare_digest(op['lease_hash'] or '',sha256(ticket['lease'].encode()).hexdigest())
    reason=authorization(c,op) if valid else 'late_fence'
    classification=result.classification
    if classification not in ('submitted','confirmed','rejected','unknown'):raise ValueError('Invalid outcome')
    bound=result.payload_sha256==op['payload_sha256'] and result.organization_id==str(op['organization_id'])
    state=classification
    if reason or not bound or (state=='confirmed' and op['semantic']=='mutation'):state='unknown'
    if state=='submitted' and not result.external_id:state='unknown'
    c.execute(text('''INSERT INTO operation_receipts(operation_id,attempt_id,fence,phase,raw,sha256,applied,classification)
        VALUES (:op,:attempt,:fence,'dispatch',:raw,:sha,:applied,:classification)'''),{'op':op['id'],'attempt':ticket['attempt_id'],'fence':ticket['fence'],'raw':result.raw,'sha':sha256(result.raw).hexdigest(),'applied':bool(valid),'classification':classification})
    if valid:
        transition(c,op['id'],state,reason or ('evidence_mismatch' if not bound else ''))
        c.execute(text('UPDATE external_operations SET external_id=:external,lease_hash=NULL,lease_until=NULL WHERE id=:id'),{'external':result.external_id if bound else None,'id':op['id']})
    return bool(valid)


def transport_failure(c,ticket,*,retry_after=5):
    op=c.execute(text('SELECT * FROM external_operations WHERE id=:id FOR UPDATE'),{'id':ticket['id']}).mappings().one()
    if op['state']!='dispatching' or op['fence']!=ticket['fence'] or not hmac.compare_digest(op['lease_hash'] or '',sha256(ticket['lease'].encode()).hexdigest()):return False
    state='queued' if op['semantic']=='read' and op['fence']<5 else 'unknown'
    transition(c,op['id'],state,'transport_error')
    delay=min(3600,max(1,int(retry_after),2**min(op['fence'],8)))+secrets.randbelow(5)
    c.execute(text('UPDATE external_operations SET available_at=clock_timestamp()+make_interval(secs=>:delay),lease_hash=NULL,lease_until=NULL WHERE id=:id'),{'delay':delay,'id':op['id']})
    return True


def reconcile(c,id,result,*,expected_fence):
    """Called only by a trusted read-only contract adapter, never a public status setter."""
    if not isinstance(result,Result) or type(result.raw) is not bytes or not 1<=len(result.raw)<=MAX_BYTES:raise ValueError('Evidence required')
    op=c.execute(text('SELECT * FROM external_operations WHERE id=:id FOR UPDATE'),{'id':id}).mappings().one()
    valid=op['state'] in ('submitted','unknown') and op['fence']==expected_fence
    bound=result.payload_sha256==op['payload_sha256'] and result.organization_id==str(op['organization_id']) and bool(result.external_id) and (op['external_id'] is None or op['external_id']==result.external_id)
    valid=valid and bound and result.classification in ('confirmed','rejected')
    c.execute(text("INSERT INTO operation_receipts(operation_id,fence,phase,raw,sha256,applied,classification) VALUES (:op,:fence,'reconcile',:raw,:sha,:valid,:classification)"),{'op':id,'fence':expected_fence,'raw':result.raw,'sha':sha256(result.raw).hexdigest(),'valid':valid,'classification':result.classification})
    if valid:
        transition(c,id,result.classification,'read_only_reconciliation')
        c.execute(text('UPDATE external_operations SET external_id=:external WHERE id=:id'),{'external':result.external_id,'id':id})
    return valid


def cancel_before_dispatch(c,id):
    c.execute(text('SELECT pg_advisory_xact_lock(683273529)'))
    op=c.execute(text('SELECT state FROM external_operations WHERE id=:id FOR UPDATE'),{'id':id}).scalar_one()
    if op not in ('queued','awaiting_signature','blocked'):raise CommandConflict('Command may already have been dispatched')
    transition(c,id,'cancelled_before_dispatch','target_cancelled')
    c.execute(text("UPDATE signer_jobs SET state='cancelled',lease_hash=NULL,lease_until=NULL WHERE operation_id=:id AND state<>'verified'"),{'id':id})


def dispatch_once(engine,worker_id,adapters):
    """Transactions finish before adapter IO; adapters are explicitly injected.

    This package has no production adapters. A missing adapter blocks the command,
    rather than inventing an endpoint or reporting success.
    """
    with engine.begin() as c:ticket=claim(c,worker_id)
    if ticket is None:return False
    adapter=adapters.get((ticket['kind'],ticket['contract_version']))
    if adapter is None:
        with engine.begin() as c:
            op=c.execute(text('SELECT state,fence,lease_hash FROM external_operations WHERE id=:id FOR UPDATE'),{'id':ticket['id']}).mappings().one()
            if op['state']=='dispatching' and op['fence']==ticket['fence'] and op['lease_hash']==sha256(ticket['lease'].encode()).hexdigest():
                transition(c,ticket['id'],'blocked','adapter_unavailable')
                c.execute(text('UPDATE external_operations SET lease_hash=NULL,lease_until=NULL WHERE id=:id'),{'id':ticket['id']})
        return False
    try:result=adapter(ticket)
    except RetryAfter as error:
        with engine.begin() as c:transport_failure(c,ticket,retry_after=error.seconds)
    except Exception:
        with engine.begin() as c:transport_failure(c,ticket)
    else:
        with engine.begin() as c:finish(c,ticket,result)
    return True

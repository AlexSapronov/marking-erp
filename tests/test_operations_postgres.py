from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from uuid import uuid4
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_auth_postgres import env,client,login
from marking_erp import operations as ops

pytestmark=[pytest.mark.postgres,pytest.mark.usefixtures('fresh_schema')]
PAYLOAD='{"данные":"точные\\r\\nбайты"}\r\n'.encode('utf-8')

@pytest.fixture
def queue_env(env,engine):
    target=uuid4()
    with engine.begin() as c:
        c.execute(text('INSERT INTO operation_targets(id,organization_id,owner_id,revision) VALUES (:id,:org,:owner,1)'),{'id':target,'org':env[1],'owner':env[3]['bm']})
        c.execute(text("INSERT INTO operation_contracts(kind,permission_id,version,semantic,requires_signature,approved,evidence_ref) VALUES (:kind,'marking.codes.issue','fixture-v1',:semantic,:signed,true,'synthetic test only')"),[
            {'kind':'fixture.write','semantic':'mutation','signed':False},{'kind':'fixture.read','semantic':'read','signed':False},{'kind':'fixture.signed','semantic':'mutation','signed':True}])
        c.execute(text('INSERT INTO operation_assignments VALUES (:target,:user)'),{'target':target,'user':env[3]['warehouse']})
    return env,target


def prepare(c,queue_env,kind='fixture.write',key='one',payload=PAYLOAD):
    env,target=queue_env
    return ops.prepare(c,organization_id=env[1],actor_id=env[3]['bm'],kind=kind,target_id=target,target_revision=1,idempotency_key=key,payload=payload)


def outcome(ticket,classification='submitted',raw=b'fixture receipt',external='fixture-id'):
    return ops.Result(raw,classification,ticket['payload_sha256'],str(ticket['organization_id']),external)


def state(c,id):return c.execute(text('SELECT state FROM external_operations WHERE id=:id'),{'id':id}).scalar_one()


def test_command_bytes_idempotency_snapshot_and_immutable_intent(queue_env,engine):
    with engine.begin() as c:
        one=prepare(c,queue_env);two=prepare(c,queue_env)
        assert one['id']==two['id'] and bytes(one['payload'])==PAYLOAD and one['payload_sha256']==sha256(PAYLOAD).hexdigest()
        assert one['policy_snapshot']['values']['archive.retention_months']==6
        with pytest.raises(ops.CommandConflict):prepare(c,queue_env,payload=PAYLOAD+b' ')
    with pytest.raises(DBAPIError),engine.begin() as c:c.execute(text("UPDATE external_operations SET payload='different'::bytea WHERE id=:id"),{'id':one['id']})
    with pytest.raises(DBAPIError),engine.begin() as c:c.execute(text('DELETE FROM external_operations'))


def test_two_workers_claim_once_and_io_has_no_open_transaction(queue_env,engine):
    with engine.begin() as c:op=prepare(c,queue_env)
    def claim(worker):
        with engine.begin() as c:return ops.claim(c,worker)
    with ThreadPoolExecutor(max_workers=2) as pool:tickets=list(pool.map(claim,['a','b']))
    assert sum(t is not None for t in tickets)==1
    ticket=next(t for t in tickets if t)
    with engine.begin() as c:assert ops.finish(c,ticket,outcome(ticket)) is True
    with engine.connect() as c:assert state(c,op['id'])=='submitted'
    with engine.begin() as c:other=prepare(c,queue_env,key='second')
    called=[]
    def adapter(ticket):
        # A fresh connection sees the committed claim before external IO begins.
        with engine.connect() as c:assert state(c,ticket['id'])=='dispatching'
        called.append(ticket['id']);return outcome(ticket)
    assert ops.dispatch_once(engine,'worker',{('fixture.write','fixture-v1'):adapter}) is True
    assert called==[other['id']]


def test_mutation_crash_is_unknown_and_late_response_is_evidence_only(queue_env,engine):
    with engine.begin() as c:op=prepare(c,queue_env);ticket=ops.claim(c,'old')
    with engine.begin() as c:c.execute(text("UPDATE external_operations SET lease_until=clock_timestamp()-interval '1 second' WHERE id=:id"),{'id':op['id']})
    with engine.begin() as c:
        assert ops.recover_expired(c)==1 and state(c,op['id'])=='unknown'
        assert ops.claim(c,'other') is None
        assert ops.finish(c,ticket,outcome(ticket)) is False
    with engine.connect() as c:
        assert state(c,op['id'])=='unknown'
        assert c.execute(text('SELECT applied FROM operation_receipts WHERE operation_id=:id'),{'id':op['id']}).scalar_one() is False


def test_read_retry_fence_rejects_old_result_and_mutation_transport_never_retries(queue_env,engine):
    with engine.begin() as c:
        read=prepare(c,queue_env,'fixture.read');old=ops.claim(c,'one');ops.transport_failure(c,old,retry_after=20)
        assert state(c,read['id'])=='queued'
        c.execute(text('UPDATE external_operations SET available_at=clock_timestamp() WHERE id=:id'),{'id':read['id']})
        new=ops.claim(c,'two');assert new['fence']==old['fence']+1
        assert ops.finish(c,old,outcome(old,'confirmed')) is False
        assert ops.finish(c,new,outcome(new,'confirmed')) is True
        assert state(c,read['id'])=='confirmed'
        mutation=prepare(c,queue_env,key='mutation');ticket=ops.claim(c,'three');ops.transport_failure(c,ticket)
        assert state(c,mutation['id'])=='unknown' and ops.claim(c,'four') is None


@pytest.mark.parametrize('change',["UPDATE users SET enabled=false WHERE group_slug='bm'", "UPDATE group_grants SET scope='none' WHERE group_slug='bm' AND permission_id='marking.codes.issue'", 'UPDATE operation_targets SET revision=2', 'UPDATE operation_targets SET stopped=true', "UPDATE operation_contracts SET approved=false"])
def test_dispatch_revalidates_current_authority_target_and_contract(queue_env,engine,change):
    with engine.begin() as c:op=prepare(c,queue_env)
    with engine.begin() as c:c.execute(text(change))
    with engine.begin() as c:assert ops.claim(c,'worker') is None and state(c,op['id'])=='blocked'


def test_receipt_not_completion_and_reconcile_is_bound_to_exact_command(queue_env,engine):
    with engine.begin() as c:
        op=prepare(c,queue_env);ticket=ops.claim(c,'one')
        ops.finish(c,ticket,outcome(ticket,'confirmed'))
        assert state(c,op['id'])=='unknown' # Mutation cannot finish from send response alone.
        wrong=ops.Result(b'wrong evidence','confirmed','0'*64,str(queue_env[0][1]),'remote')
        assert ops.reconcile(c,op['id'],wrong,expected_fence=ticket['fence']) is False
        assert ops.reconcile(c,op['id'],outcome(ticket,'confirmed'),expected_fence=ticket['fence']) is True
        assert state(c,op['id'])=='confirmed'
    for table in ('operation_attempts','operation_receipts','operation_events'):
        with pytest.raises(DBAPIError),engine.begin() as c:c.execute(text('DELETE FROM '+table))


def test_cancel_and_missing_adapter_cannot_dispatch(queue_env,engine):
    with engine.begin() as c:
        op=prepare(c,queue_env);ops.cancel_before_dispatch(c,op['id']);assert state(c,op['id'])=='cancelled_before_dispatch'
        op2=prepare(c,queue_env,key='second')
    assert ops.dispatch_once(engine,'unconfigured',{}) is False
    with engine.begin() as c:
        assert state(c,op2['id'])=='blocked'
        op3=prepare(c,queue_env,key='third');ticket=ops.claim(c,'running')
        with pytest.raises(ops.CommandConflict):ops.cancel_before_dispatch(c,op3['id'])


def test_queue_api_scope_no_payload_leak_and_no_public_retry(queue_env,engine):
    env,target=queue_env
    with engine.begin() as c:op=prepare(c,queue_env)
    for group,count in [('bm',1),('logistics',0),('warehouse',1),('specialist',1)]:
        with client(env) as c:
            login(c,group);r=c.get('/api/operations?organization_id='+str(env[1]));assert r.status_code==200 and len(r.json()['operations'])==count
            if count:
                data=c.get('/api/operations/'+str(op['id']));assert data.status_code==200 and '"payload"' not in data.text and 'lease_hash' not in data.text
            else:assert c.get('/api/operations/'+str(op['id'])).status_code==403
            assert c.get('/api/operations?organization_id='+str(env[2])).status_code==403
    with client(env) as c:
        login(c,'tsd');assert c.get('/api/operations?organization_id='+str(env[1])).status_code==403
        assert c.post('/api/operations/'+str(op['id'])+'/retry',json={}).status_code==404

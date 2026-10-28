"""Worker evidence is independent of progress and never retries business work."""
import json,threading
from uuid import uuid4
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_auth_postgres import env
from test_operations_postgres import queue_env,prepare
from marking_erp.worker_health import WorkerObserver,health,workers
from marking_erp.business.worker import BusinessWorker,background
from marking_erp.business import store
from marking_erp.business.printing import register_printer
pytestmark=[pytest.mark.postgres,pytest.mark.usefixtures('fresh_schema')]


def test_heartbeat_continues_during_blocked_stage_and_close_records_success(env,engine,monkeypatch):
    import marking_erp.worker_health as module
    monkeypatch.setattr(module,'HEARTBEAT_SECONDS',0.05)
    started=threading.Event();release=threading.Event()
    class Blocked(BusinessWorker):
        def tick(self):
            self.observer.phase('dispatch');started.set();assert release.wait(5)
    worker=Blocked(engine,env[0]);worker.start()
    try:
        assert started.wait(3)
        with engine.begin() as c:
            before=c.execute(text('SELECT heartbeat_at FROM worker_runs WHERE id=:id'),{'id':worker.observer.id}).scalar_one()
            c.execute(text("UPDATE worker_runs SET stage_started_at=now()-interval '4 minutes' WHERE id=:id"),{'id':worker.observer.id})
        assert not release.wait(.2)
        with engine.connect() as c:
            result=health(c);instance=result['workers']['instances'][0]
            assert instance['heartbeat_at']>before and instance['last_success_at'] is None
            assert result['workers']['available'] and result['workers']['delayed']==1
            assert 'worker_delayed' in {a['code'] for a in result['alerts']}
    finally:release.set();worker.close()
    with engine.connect() as c:
        row=c.execute(text('SELECT * FROM worker_runs WHERE id=:id'),{'id':worker.observer.id}).mappings().one()
        assert row['state']=='stopped' and row['last_success_at'] and row['cycles']>=1
        assert not workers(c)['available']


def test_failure_episode_recovery_and_skipped_cycle_are_redacted(env,engine):
    observer=WorkerObserver(engine,'not-a-sha PRIVATE_TOKEN');observer.start(lambda:True)
    try:
        observer.begin();observer.finish(None)
        with engine.connect() as c:
            row=c.execute(text('SELECT cycles,last_success_at FROM worker_runs')).one();assert row==(0,None)
        observer.begin();observer.phase('pipeline');observer.finish(False);observer.finish(False)
        with engine.connect() as c:
            result=health(c);row=result['workers']['instances'][0]
            assert row['consecutive_errors']==2 and row['errors']==2 and row['build_sha'] is None
            assert result['workers']['failing']==1
            assert [e['action'] for e in result['workers']['events']].count('cycle_failed')==1
        observer.begin();observer.finish(True)
        with engine.connect() as c:
            result=health(c);assert result['workers']['failing']==0
            assert result['workers']['instances'][0]['last_success_at']
            assert 'recovered' in [e['action'] for e in result['workers']['events']]
            assert 'PRIVATE_TOKEN' not in json.dumps(result,default=str)
    finally:observer.close()
    with pytest.raises(DBAPIError),engine.begin() as c:c.execute(text('DELETE FROM worker_events'))
    with pytest.raises(DBAPIError),engine.begin() as c:c.execute(text('DELETE FROM worker_runs'))


def test_uncaught_cycle_exception_is_recorded_without_raw_exception(env,engine):
    class Broken(BusinessWorker):
        def tick(self):
            self.stop.set();raise RuntimeError('SECRET_TOKEN postgresql://private:password@host')
    worker=Broken(engine,env[0]);worker.start();worker.thread.join(4);assert not worker.thread.is_alive()
    with engine.connect() as c:
        result=health(c);assert result['workers']['instances'][0]['errors']==1
        assert 'cycle_failed' in [e['action'] for e in result['workers']['events']]
        assert 'SECRET' not in json.dumps(result,default=str) and 'postgresql' not in json.dumps(result,default=str)


def test_restored_observation_cannot_be_revived_by_old_heartbeat(env,engine):
    from marking_erp.backup import _freeze
    observer=WorkerObserver(engine,'a'*40);observer.start(lambda:True)
    try:
        observer.begin();observer.finish(True)
        with engine.begin() as c:_freeze(c,'0'*64,'2026-10-28T00:00:00Z')
        observer.phase('dispatch');observer.finish(True)
        with engine.connect() as c:
            result=health(c);assert not result['workers']['available']
            row=result['workers']['instances'][0];assert row['state']=='restored' and row['heartbeat_at'] is None
            assert 'restored' in [e['action'] for e in result['workers']['events']]
    finally:observer.close()


def test_recent_stopped_instances_do_not_hide_live_old_worker(env,engine):
    with engine.begin() as c:
        c.execute(text("INSERT INTO worker_runs(id,started_at,heartbeat_at,thread_alive,state,stage,last_success_at) VALUES (:id,now()-interval '30 days',now(),true,'idle','idle',now())"),{'id':uuid4()})
        for _ in range(25):c.execute(text("INSERT INTO worker_runs(id,state,stage,stopped_at) VALUES (:id,'stopped','stopped',now())"),{'id':uuid4()})
    with engine.connect() as c:
        result=health(c);assert result['workers']['available'] and result['workers']['truncated']
        assert 'worker_unavailable' not in {a['code'] for a in result['alerts']}


def test_queue_warnings_exclude_future_backoff_and_do_not_mutate_operations(queue_env,engine):
    env,_=queue_env
    with engine.begin() as c:
        ready=prepare(c,queue_env,'fixture.read','ready')
        future=prepare(c,queue_env,'fixture.read','future')
        unknown=prepare(c,queue_env,'fixture.read','unknown')
        waiting=prepare(c,queue_env,'fixture.read','receipt')
        expired=prepare(c,queue_env,'fixture.read','lease')
        c.execute(text("UPDATE external_operations SET available_at=now()-interval '6 minutes' WHERE id=:id"),{'id':ready['id']})
        c.execute(text("UPDATE external_operations SET available_at=now()+interval '1 hour' WHERE id=:id"),{'id':future['id']})
        c.execute(text("UPDATE external_operations SET state='unknown',reason='PRIVATE_REMOTE_RESPONSE' WHERE id=:id"),{'id':unknown['id']})
        c.execute(text("UPDATE external_operations SET state='submitted',updated_at=now()-interval '11 minutes' WHERE id=:id"),{'id':waiting['id']})
        c.execute(text("UPDATE external_operations SET state='dispatching',lease_until=now()-interval '1 second' WHERE id=:id"),{'id':expired['id']})
    with engine.connect() as c:
        result=health(c);alerts={a['code']:a for a in result['alerts']}
        assert alerts['ready_queue_delayed']['count']==1
        assert alerts['unknown_operations']['count']==1 and alerts['dispatch_lease_expired']['count']==1
        assert alerts['external_receipt_delayed']['count']==1
        assert 'PRIVATE_REMOTE_RESPONSE' not in json.dumps(result,default=str) and str(unknown['id']) not in json.dumps(result,default=str)
        assert c.execute(text('SELECT state FROM external_operations WHERE id=:id'),{'id':unknown['id']}).scalar_one()=='unknown'
        assert c.execute(text('SELECT count(*) FROM operation_attempts')).scalar_one()==0


def test_print_catalog_and_authorization_alerts_are_local_only(env,engine):
    with engine.begin() as c:
        actor=background(c,env[3]['specialist'],env[1]);item=store.create(actor,'marking','PRIVATE_TITLE',{},env[1])
        file=store.artifact(actor,item,'labels_pdf','private.pdf',b'%PDF fixture','application/pdf')
        printer,_=register_printer(c,env[1],'PRIVATE_PRINTER','PRIVATE_DRIVER')
        for state in ('queued','claimed','held','unknown'):
            c.execute(text("INSERT INTO print_jobs(id,organization_id,actor_id,device_id,artifact_id,copies,request_sha256,state,created_at,claimed_at) VALUES (:id,:org,:actor,:device,:file,1,:sha,:state,now()-interval '6 minutes',now()-interval '6 minutes')"),{'id':uuid4(),'org':env[1],'actor':actor.id,'device':printer,'file':file,'sha':'0'*64,'state':state})
        sync=store.create(actor,'sync','PRIVATE_SYNC',{},env[1],state='processing')
        c.execute(text("UPDATE business_items SET updated_at=now()-interval '11 minutes' WHERE id=:id"),{'id':sync['id']})
        store.create(actor,'import','PRIVATE_IMPORT',{},env[1],state='blocked')
        c.execute(text("INSERT INTO auto_print_requests(id,item_id,organization_id,actor_id,device_id,request_sha256,state) VALUES (:id,:item,:org,:actor,:device,:sha,'blocked')"),{'id':uuid4(),'item':item['id'],'org':env[1],'actor':actor.id,'device':printer,'sha':'0'*64})
        c.execute(text("INSERT INTO integration_auth_jobs(id,organization_id,actor_id,service,challenge_uuid,challenge,payload_sha256,connection_revision,expires_at) VALUES (:id,:org,:actor,'true',:challenge,:data,:sha,1,now()-interval '1 second')"),{'id':uuid4(),'org':env[1],'actor':actor.id,'challenge':uuid4(),'data':b'PRIVATE_CHALLENGE','sha':'0'*64})
    with engine.connect() as c:
        result=health(c);alerts={a['code']:a for a in result['alerts']}
        assert alerts['print_unknown']['count']==2 and alerts['print_queue_delayed']['count']==1
        assert alerts['print_held']['count']==1 and alerts['auto_print_blocked']['count']==1
        assert alerts['catalog_processing_delayed']['count']==1 and alerts['catalog_blocked']['count']==1
        assert alerts['authorization_expired']['count']==1
        assert 'PRIVATE' not in json.dumps(result,default=str)
        assert c.execute(text("SELECT count(*) FROM print_jobs WHERE state IN ('claimed','unknown')")).scalar_one()==2

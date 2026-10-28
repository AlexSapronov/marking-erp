"""Real overlapping HTTP transactions: independent work and conflicting writes."""
from concurrent.futures import ThreadPoolExecutor
from time import monotonic,sleep
from uuid import uuid4
import pytest
from sqlalchemy import text
from test_auth_postgres import env,client,login
from test_documents_postgres import body as invoice,create
from test_warehouse_postgres import setup,scan,raw
from marking_erp.write_locks import FENCE

pytestmark=[pytest.mark.postgres,pytest.mark.usefixtures('fresh_schema')]


@pytest.fixture(autouse=True)
def explicit_worker(monkeypatch):
    # Exercise worker contenders explicitly below; no timer can race assertions.
    monkeypatch.setattr('marking_erp.business.worker.BusinessWorker.start',lambda self:None)


def wait_for_blocker(engine,pid):
    deadline=monotonic()+5
    while monotonic()<deadline:
        with engine.connect() as c:
            waiting=c.execute(text('SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE :pid=ANY(pg_blocking_pids(pid)))'),{'pid':pid}).scalar_one()
        if waiting:return
        sleep(.02)
    pytest.fail('Request did not reach the held business row')


def test_locked_invoice_edit_does_not_block_another_invoice(env,engine):
    with client(env) as a,client(env) as b:
        login(a);login(b);first,value=create(a,env);second,other=create(b,env,number='Independent')
        def edit(c,doc,data):
            return c.put('/api/documents/'+doc['id'],json={**{k:v for k,v in data.items() if k!='request_id'},'revision':1})
        with engine.connect() as hold,ThreadPoolExecutor(max_workers=2) as pool:
            tx=hold.begin();pid=hold.execute(text('SELECT pg_backend_pid()')).scalar_one()
            hold.execute(text('SELECT id FROM bm_documents WHERE id=:id FOR UPDATE'),{'id':first['id']})
            try:
                blocked=pool.submit(edit,a,first,value);wait_for_blocker(engine,pid)
                response=pool.submit(edit,b,second,other).result(timeout=3)
                assert response.status_code==200,response.text
                assert not blocked.done()
            finally:tx.rollback()
            assert blocked.result(timeout=3).status_code==200


def test_waiting_close_does_not_block_other_scan_or_bm_edit(env,engine):
    first,number,_=setup(env,labels=1);second,_,_=setup(env,labels=1)
    with client(env) as a,client(env) as b,client(env) as bm:
        login(a,'tsd');login(b,'tsd');login(bm)
        assert scan(a,first).status_code==200
        revision=a.get('/api/warehouse/tasks/'+first).json()['task']['revision']
        doc,value=create(bm,env,number='BM-independent')
        with engine.connect() as hold,ThreadPoolExecutor(max_workers=3) as pool:
            tx=hold.begin();pid=hold.execute(text('SELECT pg_backend_pid()')).scalar_one()
            hold.execute(text('SELECT id FROM warehouse_tasks WHERE id=:id FOR UPDATE'),{'id':first})
            try:
                blocked=pool.submit(a.post,'/api/warehouse/tasks/'+first+'/close',json={'revision':revision,'confirmation':number})
                wait_for_blocker(engine,pid)
                response=pool.submit(scan,b,second,{'request_id':str(uuid4()),'raw':raw('b')}).result(timeout=3)
                assert response.status_code==200,response.text
                response=pool.submit(bm.put,'/api/documents/'+doc['id'],json={**{k:v for k,v in value.items() if k!='request_id'},'revision':1}).result(timeout=3)
                assert response.status_code==200,response.text
                assert not blocked.done()
            finally:tx.rollback()
            assert blocked.result(timeout=3).status_code==200


@pytest.mark.parametrize('conflict',['request','header','tnved'])
def test_parallel_invoice_creation_preserves_identity_and_invariants(env,engine,conflict):
    first=invoice(env);second={**first}
    if conflict!='request':second['request_id']=str(uuid4())
    if conflict=='tnved':
        second['number']='Other header'
        second['lines']=[{**first['lines'][0],'tnved':'8536699008'}]
    with client(env) as a,client(env) as b,ThreadPoolExecutor(max_workers=2) as pool:
        login(a);login(b)
        futures=[pool.submit(a.post,'/api/documents',json=first),pool.submit(b.post,'/api/documents',json=second)]
        results=[f.result(timeout=10) for f in futures]
        assert sorted(r.status_code for r in results)==([200,200] if conflict=='request' else [200,409])
        if conflict=='request':assert sum(bool(r.json().get('replayed')) for r in results)==1
    with engine.connect() as c:
        assert c.execute(text('SELECT count(*) FROM bm_documents')).scalar_one()==1
        assert c.execute(text('SELECT count(*) FROM bm_document_versions')).scalar_one()==1


def test_request_uuid_collision_across_tasks_is_conflict_not_server_error(env,engine):
    first,_,_=setup(env,labels=1);second,_,_=setup(env,labels=1);request=str(uuid4())
    with client(env) as a,client(env) as b,ThreadPoolExecutor(max_workers=2) as pool:
        login(a,'tsd');login(b,'tsd')
        futures=[pool.submit(scan,a,first,{'request_id':request,'raw':raw()}),pool.submit(scan,b,second,{'request_id':request,'raw':raw('b')})]
        assert sorted(f.result(timeout=10).status_code for f in futures)==[200,409]
    with engine.connect() as c:assert c.execute(text('SELECT count(*) FROM warehouse_scans')).scalar_one()==1


def test_revocation_waits_for_current_write_then_rejects_new_requests(env,engine):
    id,_,_=setup(env,labels=2)
    with client(env) as tsd,client(env) as admin:
        login(tsd,'tsd');login(admin,'administrator')
        with engine.connect() as hold,ThreadPoolExecutor(max_workers=2) as pool:
            tx=hold.begin();pid=hold.execute(text('SELECT pg_backend_pid()')).scalar_one()
            hold.execute(text('SELECT id FROM warehouse_tasks WHERE id=:id FOR UPDATE'),{'id':id})
            try:
                active=pool.submit(scan,tsd,id);wait_for_blocker(engine,pid)
                revoke=pool.submit(admin.post,'/api/users/'+str(env[3]['tsd'])+'/revoke-sessions')
                # Observe a real exclusive fence waiter rather than relying on a sleep.
                deadline=monotonic()+5;waiting=False
                while monotonic()<deadline:
                    with engine.connect() as c:
                        waiting=c.execute(text("SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype='advisory' AND objid=:key AND mode='ExclusiveLock' AND NOT granted)"),{'key':FENCE}).scalar_one()
                    if waiting:break
                    sleep(.02)
                assert waiting and not revoke.done()
            finally:tx.rollback()
            assert active.result(timeout=3).status_code==200
            assert revoke.result(timeout=3).status_code==200
        assert scan(tsd,id,{'request_id':str(uuid4()),'raw':raw('b')}).status_code==401


def test_idle_worker_steps_do_not_queue_exclusive_fence_behind_local_write(env,engine):
    from marking_erp.business.catalog_schedule import tick as schedule
    from marking_erp.business.auto_print import tick as printing
    from marking_erp.business.worker import BusinessWorker
    from marking_erp.operations import claim
    worker=BusinessWorker(engine,env[0])
    def contenders():
        schedule(engine,env[0]);printing(engine);worker.pipeline_tick()
        with engine.begin() as c:assert claim(c,'fixture-contender') is None
    with engine.begin() as hold,ThreadPoolExecutor(max_workers=1) as pool:
        hold.execute(text('SELECT pg_advisory_xact_lock_shared(:key)'),{'key':FENCE})
        pool.submit(contenders).result(timeout=3)


def test_large_resource_set_uses_row_locks_not_one_advisory_slot_per_cis(engine):
    from marking_erp.write_locks import resources
    # Exceeds the ordinary shared advisory lock pool; a legal large supply must
    # not fail with out-of-shared-memory. No codes leave this isolated database.
    keys=['synthetic-cis-'+str(n) for n in range(20000)]
    with engine.begin() as c:
        resources(c,'fixture-large',keys,rows=True)
        resources(c,'fixture-large',list(reversed(keys)),rows=True)
        assert c.execute(text('SELECT count(*) FROM write_resources')).scalar_one()==20000
        assert c.execute(text("SELECT count(*) FROM pg_locks WHERE pid=pg_backend_pid() AND locktype='advisory'")).scalar_one()==0
        assert c.execute(text("SELECT count(*) FROM write_resources WHERE key LIKE '%synthetic%'")).scalar_one()==0


def test_read_lock_on_source_does_not_serialize_warehouse_scans(env,engine):
    id,_,source=setup(env,labels=1)
    with client(env) as c,engine.connect() as hold,ThreadPoolExecutor(max_workers=1) as pool:
        login(c,'tsd');tx=hold.begin()
        hold.execute(text('SELECT id FROM bm_documents WHERE id=:id FOR SHARE'),{'id':source})
        try:
            response=pool.submit(scan,c,id).result(timeout=3)
            assert response.status_code==200,response.text
        finally:tx.rollback()

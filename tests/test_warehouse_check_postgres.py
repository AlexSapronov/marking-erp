"""Local identity before network, one batch read, fresh evidence and safe replay."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4
import json
import pytest
from sqlalchemy import text
from test_auth_postgres import env,client,login
from test_documents_postgres import body as invoice
from test_warehouse_postgres import raw,scan,GTIN
from test_business_postgres import connections
from marking_erp.business.live import Live

pytestmark=[pytest.mark.postgres,pytest.mark.usefixtures('fresh_schema')]


def enable(env,**extra):
    with client(env) as c:
        login(c,'administrator');connections(c,env,true_token='fixture-token',warehouse_live_checks=True,**extra)


def task(env,codes=None,*,kind='receipt',labels=None):
    codes=codes if codes is not None else [raw('a'),raw('b')]
    number='BATCH-'+uuid4().hex[:8]
    with client(env) as c:
        login(c);created=c.post('/api/documents',json=invoice(env,number=number,lines=[{'pn':'PN-01','gtin':GTIN,'tnved':'8542399000','quantity':10,'labels':labels or len(codes)}]));assert created.status_code==200,created.text
        source=created.json()['id'];assert c.post('/api/documents/'+source+'/prepare',json={'revision':1}).status_code==200
    request={'request_id':str(uuid4()),'source_id':source,'source_revision':2,'confirmed':True,'kind':kind,'assignees':[str(env[3]['tsd'])],'expected_codes':codes}
    with client(env) as c:
        login(c,'logistics');created=c.post('/api/logistics/warehouse-transfer',json=request);assert created.status_code==200,created.text
    return created.json()['id'],number,request


def check(c,id):
    revision=c.get('/api/warehouse/tasks/'+id).json()['task']['revision']
    body={'request_id':str(uuid4()),'revision':revision}
    response=c.post('/api/warehouse/tasks/'+id+'/check',json=body)
    assert response.status_code==200,response.text
    return response.json(),body


def fake_info(calls,*,status='APPLIED',owner='7700000000',quantity=10):
    def info(self,cises,*,timeout=None):
        assert timeout is None or 0<timeout<=40
        calls.append(list(cises))
        return {cis:{'gtin':cis[2:16],'status':status,'ownerInn':owner,'quantityInPack':quantity} for cis in cises}
    return info


def test_scanning_never_calls_chz_and_rejects_foreign_cis_or_changed_crypto(env,engine,monkeypatch):
    enable(env);id,number,_=task(env);calls=[];monkeypatch.setattr(Live,'info',fake_info(calls))
    with client(env) as c:
        login(c,'tsd')
        for value in (raw('c'),raw('a').replace('93ABCD','93WXYZ'),raw(gtin='04640638341173')):
            assert scan(c,id,{'request_id':str(uuid4()),'raw':value}).status_code==422
        first={'request_id':str(uuid4()),'raw':raw('a')};assert scan(c,id,first).status_code==200
        assert scan(c,id).status_code==409
        assert scan(c,id,{'request_id':str(uuid4()),'raw':raw('b')}).status_code==200
        assert not calls
        before=c.get('/api/warehouse/tasks/'+id).json()
        assert c.post('/api/warehouse/tasks/'+id+'/close',json={'revision':before['task']['revision'],'confirmation':number}).status_code==409
        result,body=check(c,id);assert result['ok'] and result['external_verified']
        assert len(calls)==1 and len(calls[0])==2
        assert c.post('/api/warehouse/tasks/'+id+'/check',json=body).json()['replayed']
        data=c.get('/api/warehouse/tasks/'+id).json();assert data['check_fresh'] and data['expectation_level']=='full'
        assert c.post('/api/warehouse/tasks/'+id+'/close',json={'revision':data['task']['revision'],'confirmation':number}).status_code==200
        assert len(calls)==1
        assert scan(c,id,first).json()['replayed']
        assert c.post('/api/warehouse/tasks/'+id+'/check',json=body).json()['replayed']
    with engine.connect() as db:
        assert db.execute(text("SELECT count(*) FROM warehouse_events WHERE action='checked'")).scalar_one()==1
        assert db.execute(text('SELECT count(*) FROM external_operations')).scalar_one()==0


def test_incomplete_check_is_local_and_failed_remote_facts_cannot_close(env,monkeypatch):
    enable(env);id,number,_=task(env);calls=[];monkeypatch.setattr(Live,'info',fake_info(calls,owner='OTHER'))
    with client(env) as c:
        login(c,'tsd');assert scan(c,id).status_code==200
        result,_=check(c,id);assert not result['ok'] and result['missing_count']==1 and not calls
        assert scan(c,id,{'request_id':str(uuid4()),'raw':raw('b')}).status_code==200
        result,_=check(c,id);assert not result['ok'] and not result['external_verified'] and len(calls)==1
        data=c.get('/api/warehouse/tasks/'+id).json();assert not data['check_fresh'] and not data['external_verified']
        assert c.post('/api/warehouse/tasks/'+id+'/close',json={'revision':data['task']['revision'],'confirmation':number}).status_code==409
        assert len(calls)==1


def test_removed_scan_and_old_request_receipt_cannot_restore_stale_check(env,monkeypatch):
    enable(env);id,_,_=task(env,codes=[raw('a')]);calls=[];monkeypatch.setattr(Live,'info',fake_info(calls))
    with client(env) as c:
        login(c,'tsd');saved=scan(c,id).json()['id'];result,body=check(c,id);assert result['ok']
        assert c.delete('/api/warehouse/tasks/'+id+'/scans/'+saved).status_code==200
        assert c.post('/api/warehouse/tasks/'+id+'/check',json=body).json()['replayed']
        data=c.get('/api/warehouse/tasks/'+id).json();assert not data['check_fresh'] and not data['external_verified'] and data['task']['check_result'] is None
        assert scan(c,id).status_code==200
        assert not c.get('/api/warehouse/tasks/'+id).json()['check_fresh']
        check(c,id);assert len(calls)==2


def test_expired_facts_and_policy_change_require_another_explicit_check(env,engine,monkeypatch):
    enable(env);id,number,_=task(env,codes=[raw('a')]);calls=[];monkeypatch.setattr(Live,'info',fake_info(calls))
    with client(env) as c:
        login(c,'tsd');scan(c,id);check(c,id)
        with engine.begin() as db:db.execute(text("UPDATE warehouse_tasks SET check_result=jsonb_set(check_result,'{checked_at}',to_jsonb((now()+interval '1 hour')::text)) WHERE id=:id"),{'id':id})
        assert not c.get('/api/warehouse/tasks/'+id).json()['check_fresh']
        with engine.begin() as db:db.execute(text("UPDATE warehouse_tasks SET check_result=jsonb_set(check_result,'{checked_at}',to_jsonb((now()-interval '1 hour')::text)) WHERE id=:id"),{'id':id})
        data=c.get('/api/warehouse/tasks/'+id).json();assert not data['check_fresh']
        assert c.post('/api/warehouse/tasks/'+id+'/close',json={'revision':data['task']['revision'],'confirmation':number}).status_code==409
        assert len(calls)==1
        check(c,id)
        with engine.begin() as db:db.execute(text("UPDATE integration_settings SET public=jsonb_set(public,'{ignore_legal_entity}','true') WHERE organization_id=:org"),{'org':env[1]})
        assert not c.get('/api/warehouse/tasks/'+id).json()['check_fresh']


def test_check_scope_revoke_and_transfer_replay_bind_expected_input(env,engine):
    id,_,request=task(env,codes=[raw('a')])
    with client(env) as c,client(env) as log:
        login(c,'warehouse');assert c.post('/api/warehouse/tasks/'+id+'/check',json={'request_id':str(uuid4()),'revision':2}).status_code==403
        login(log,'logistics');assert log.post('/api/logistics/warehouse-transfer',json=request).json()['replayed']
        assert log.post('/api/logistics/warehouse-transfer',json={**request,'expected_codes':[raw('b')]}).status_code==409
    with client(env) as c:
        login(c,'tsd');result,body=check(c,id)
        with engine.begin() as db:db.execute(text("UPDATE group_grants SET scope='none' WHERE group_slug='tsd' AND permission_id='warehouse.receipt.check'"))
        assert c.post('/api/warehouse/tasks/'+id+'/check',json=body).status_code==403


def test_remote_check_of_one_task_does_not_block_scanning_another(env,monkeypatch):
    enable(env);first,_,_=task(env,codes=[raw('a')]);second,_,_=task(env,codes=[raw('b')])
    started=Event();release=Event();calls=[];basic=fake_info(calls)
    def waiting(self,cises,**kwargs):
        started.set();assert release.wait(8);return basic(self,cises,**kwargs)
    monkeypatch.setattr(Live,'info',waiting)
    with client(env) as a,client(env) as b:
        login(a,'tsd');login(b,'tsd');assert scan(a,first).status_code==200
        with ThreadPoolExecutor(max_workers=2) as pool:
            running=pool.submit(check,a,first);assert started.wait(5)
            other=pool.submit(scan,b,second,{'request_id':str(uuid4()),'raw':raw('b')})
            try:assert other.result(timeout=3).status_code==200
            finally:release.set()
            assert running.result(timeout=5)[0]['ok']


def test_501_codes_are_verified_in_bounded_http_batches(env,engine,monkeypatch):
    enable(env);codes=['01'+GTIN+'21'+f'{n:020d}'+'\x1d93ABCD\x1d3010' for n in range(501)]
    id,_,_=task(env,codes=codes);calls=[]
    def call(self,method,path,params=None,body=None,**kwargs):
        assert method=='POST' and path=='/api/v3/true-api/cises/info'
        values=json.loads(body);assert len(values)<=500;calls.append(values)
        return [{'cisInfo':{'requestedCis':cis,'gtin':GTIN,'status':'APPLIED','ownerInn':'7700000000','quantityInPack':10}} for cis in values]
    monkeypatch.setattr(Live,'call',call)
    with engine.begin() as db:
        db.execute(text('INSERT INTO warehouse_scans(id,task_id,line_index,cis,gtin,quantity,payload,actor_id) VALUES (:id,:task,0,:cis,:gtin,10,:payload,:actor)'),
            [{'id':uuid4(),'task':id,'cis':code[:38],'gtin':GTIN,'payload':code.encode(),'actor':env[3]['tsd']} for code in codes])
    with client(env) as c:
        login(c,'tsd');result,_=check(c,id);assert result['ok'] and result['external_verified']
    assert [len(v) for v in calls]==[500,1]
    assert set(calls[0]+calls[1])=={code[:38] for code in codes}


@pytest.mark.parametrize('kind,status,quantity,ok',[
    ('receipt','EMITTED',10,False),('receipt','APPLIED',9,False),
    ('shipment','APPLIED',10,False),('shipment','INTRODUCED',10,True)])
def test_batch_enforces_kind_status_and_pack_quantity(env,monkeypatch,kind,status,quantity,ok):
    enable(env);id,number,_=task(env,codes=[raw('a')],kind=kind);calls=[]
    monkeypatch.setattr(Live,'info',fake_info(calls,status=status,quantity=quantity))
    with client(env) as c:
        login(c,'tsd');assert scan(c,id).status_code==200
        result,_=check(c,id);assert result['ok'] is ok and result['external_verified'] is ok
        data=c.get('/api/warehouse/tasks/'+id).json()
        response=c.post('/api/warehouse/tasks/'+id+'/close',json={'revision':data['task']['revision'],'confirmation':number})
        assert response.status_code==(200 if ok else 409)
        assert len(calls)==1


def test_remote_response_after_check_budget_is_not_accepted(env,monkeypatch):
    from types import SimpleNamespace
    from marking_erp import warehouse_routes
    enable(env);id,_,_=task(env,codes=[raw('a')]);calls=[]
    monkeypatch.setattr(Live,'info',fake_info(calls))
    clock=iter([0,0,1000]);monkeypatch.setattr(warehouse_routes,'time',SimpleNamespace(monotonic=lambda:next(clock)))
    with client(env) as c:
        login(c,'tsd');scan(c,id);result,_=check(c,id)
        assert not result['ok'] and not result['external_verified'] and 'срок' in result['message']
        assert not c.get('/api/warehouse/tasks/'+id).json()['check_fresh']


def test_restore_invalidates_open_check_but_preserves_historical_events(env,engine):
    from marking_erp.backup import _freeze
    id,_,_=task(env,codes=[raw('a')])
    with client(env) as c:
        login(c,'tsd');scan(c,id);result,_=check(c,id);assert result['ok']
    with engine.begin() as db:
        _freeze(db,'1'*64,'fixture-snapshot')
        row=db.execute(text('SELECT check_result,check_digest,verified_at FROM warehouse_tasks WHERE id=:id'),{'id':id}).mappings().one()
        assert row['check_result'] is None and row['check_digest'] is None and row['verified_at'] is None
        assert db.execute(text("SELECT count(*) FROM warehouse_events WHERE task_id=:id AND action='checked'"),{'id':id}).scalar_one()==1

"""A physical KM cannot belong to two incompatible open invoice tasks."""
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
import pytest
from sqlalchemy import text
from test_auth_postgres import env,client,login
from test_documents_postgres import body as invoice
from test_warehouse_postgres import raw,scan,setup,GTIN

pytestmark=[pytest.mark.postgres,pytest.mark.usefixtures('fresh_schema')]


def source(env):
    with client(env) as c:
        login(c);body=invoice(env,number='OVERLAP-'+uuid4().hex[:8],lines=[{'pn':'PN-01','gtin':GTIN,'tnved':'8542399000','quantity':10,'labels':1}])
        response=c.post('/api/documents',json=body);assert response.status_code==200,response.text
        id=response.json()['id'];assert c.post('/api/documents/'+id+'/prepare',json={'revision':1}).status_code==200
        return id,body['number']


def request(env,id,*,kind='receipt',codes=None):
    return {'request_id':str(uuid4()),'source_id':id,'source_revision':2,'confirmed':True,'kind':kind,'assignees':[str(env[3]['tsd'])],'expected_codes':codes if codes is not None else [raw()]}


def transfer(c,body):return c.post('/api/logistics/warehouse-transfer',json=body)


def transition(c,id,number,action):
    data=c.get('/api/warehouse/tasks/'+id+'?logistics=true').json()
    return c.post('/api/warehouse/tasks/'+id+'/'+action,json={'revision':data['task']['revision'],'confirmation':number,'reason':'Проверка состава поставки'})


def test_transfer_overlap_and_cancel_release_keep_history(env,engine):
    first,number=source(env);second,_=source(env)
    with client(env) as c:
        login(c,'logistics');body=request(env,first);created=transfer(c,body);assert created.status_code==200,created.text
        id=created.json()['id'];assert transfer(c,body).json()['replayed']
        other=request(env,second,kind='shipment',codes=[raw().split('\x1d')[0]])
        denied=transfer(c,other);assert denied.status_code==409 and 'другой активной' in denied.text
        assert first not in denied.text and id not in denied.text
        assert transition(c,id,number,'cancel').status_code==200
        assert transfer(c,other).status_code==200
    with engine.connect() as db:
        assert db.execute(text('SELECT expected_codes FROM warehouse_tasks WHERE id=:id'),{'id':id}).scalar_one()
        assert db.execute(text("SELECT count(*) FROM warehouse_events WHERE task_id=:id AND action='cancel'"),{'id':id}).scalar_one()==1


def test_same_source_receipt_and_shipment_compatible(env):
    id,_=source(env)
    with client(env) as c:
        login(c,'logistics')
        for kind in ('receipt','shipment'):
            created=transfer(c,request(env,id,kind=kind));assert created.status_code==200,created.text


def test_concurrent_transfers_only_one_wins(env):
    first,_=source(env);second,_=source(env)
    with client(env) as a,client(env) as b:
        login(a,'logistics');login(b,'logistics')
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(transfer,a,request(env,first)),pool.submit(transfer,b,request(env,second))]
            assert sorted(f.result(timeout=10).status_code for f in futures)==[200,409]


def test_legacy_scans_cross_task_conflict_are_serialized(env):
    first,_,_=setup(env,labels=1);second,_,_=setup(env,labels=1)
    with client(env) as a,client(env) as b:
        login(a,'tsd');login(b,'tsd')
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(scan,a,first),pool.submit(scan,b,second)]
            responses=[f.result(timeout=10) for f in futures]
            assert sorted(r.status_code for r in responses)==[200,409]
        winner=first if responses[0].status_code==200 else second
        loser=second if winner==first else first
        data=a.get('/api/warehouse/tasks/'+winner).json()
        accepted=data['scans'][0]['id'];assert a.delete('/api/warehouse/tasks/'+winner+'/scans/'+accepted).status_code==200
        assert scan(a,loser).status_code==200


def test_expected_list_blocks_legacy_scan_and_legacy_scan_blocks_transfer(env):
    first,_=source(env);legacy,_,_=setup(env,labels=1)
    with client(env) as c:
        login(c,'logistics');created=transfer(c,request(env,first));assert created.status_code==200
    with client(env) as c:
        login(c,'tsd');assert scan(c,legacy).status_code==409
        assert scan(c,legacy,{'request_id':str(uuid4()),'raw':raw('b')}).status_code==200
    second,_=source(env)
    with client(env) as c:
        login(c,'logistics');assert transfer(c,request(env,second,codes=[raw('b')])).status_code==409


def test_closed_task_releases_codes_but_cannot_reopen_over_another_source(env):
    first,number=source(env);second,_=source(env)
    with client(env) as c:
        login(c,'logistics');created=transfer(c,request(env,first));assert created.status_code==200
        id=created.json()['id']
    with client(env) as c:
        login(c,'tsd');saved={'request_id':str(uuid4()),'raw':raw()};assert scan(c,id,saved).status_code==200
        data=c.get('/api/warehouse/tasks/'+id).json();assert c.post('/api/warehouse/tasks/'+id+'/close',json={'revision':data['task']['revision'],'confirmation':number}).status_code==200
    with client(env) as c:
        login(c,'logistics');assert transfer(c,request(env,second)).status_code==200
    with client(env) as c:
        login(c,'specialist');assert transition(c,id,number,'reopen').status_code==409
    with client(env) as c:
        login(c,'tsd');assert scan(c,id,saved).json()['replayed']


def test_existing_overlap_blocks_check_and_close_without_mutating_expectations(env,engine):
    first,number=source(env);legacy,_,_=setup(env,labels=1)
    with client(env) as c:
        login(c,'logistics');created=transfer(c,request(env,first));assert created.status_code==200
        id=created.json()['id']
    with client(env) as c:
        login(c,'tsd');assert scan(c,id).status_code==200
    # A pre-upgrade overlap is retained; the migration must not delete history.
    with engine.begin() as db:
        db.execute(text("UPDATE warehouse_tasks SET expected_codes=CAST(:codes AS jsonb) WHERE id=:id"),{'id':legacy,'codes':'["'+raw().split('\x1d')[0]+'"]'})
    with client(env) as c:
        login(c,'tsd');data=c.get('/api/warehouse/tasks/'+id).json();revision=data['task']['revision']
        assert c.post('/api/warehouse/tasks/'+id+'/check',json={'request_id':str(uuid4()),'revision':revision}).status_code==409
        assert c.post('/api/warehouse/tasks/'+id+'/close',json={'revision':revision,'confirmation':number}).status_code==409
        assert c.get('/api/warehouse/tasks/'+id).json()['task']['state']=='open'

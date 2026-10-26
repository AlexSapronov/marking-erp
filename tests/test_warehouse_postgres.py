from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta,timezone
from uuid import UUID,uuid4
import pytest
from sqlalchemy import text
from test_auth_postgres import env,client,login
from test_documents_postgres import body as invoice_body

pytestmark=[pytest.mark.postgres,pytest.mark.usefixtures('fresh_schema')]
GTIN='04640638349704'

def setup(env,labels=2,kind='receipt'):
    with client(env) as bm:
        login(bm);value=invoice_body(env,number='WH-'+uuid4().hex[:8],lines=[{'pn':'PN-01','gtin':GTIN,'tnved':'8542399000','quantity':10,'labels':labels}])
        doc=bm.post('/api/documents',json=value);assert doc.status_code==200,doc.text
        id=doc.json()['id'];assert bm.post('/api/documents/'+id+'/prepare',json={'revision':1}).status_code==200
    with client(env) as log:
        login(log,'logistics');body={'request_id':str(uuid4()),'source_id':id,'source_revision':2,'kind':kind,'assignees':[str(env[3]['tsd'])]}
        task=log.post('/api/logistics/warehouse-transfer',json=body);assert task.status_code==200,task.text
        assert log.post('/api/logistics/warehouse-transfer',json=body).json()['replayed']
    return task.json()['id'],value['number'],id

def raw(serial='a',gtin=GTIN):return '01'+gtin+'21'+serial*20+'\x1d93ABCD\x1d3010'

def scan(c,id,value=None):return c.post('/api/warehouse/tasks/'+id+'/scans',json=value or {'request_id':str(uuid4()),'raw':raw()})

def test_receipt_lifecycle_and_replay_after_close(env):
    id,number,source=setup(env,labels=1)
    with client(env) as c:
        login(c,'tsd');value={'request_id':str(uuid4()),'raw':raw()};saved=scan(c,id,value);assert saved.status_code==200,saved.text
        data=c.get('/api/warehouse/tasks/'+id).json();assert data['lines'][0]['units']==10
        assert c.post('/api/warehouse/tasks/'+id+'/close',json={'revision':data['task']['revision'],'confirmation':number}).status_code==200
        replay=scan(c,id,value);assert replay.status_code==200 and replay.json()['replayed']
        assert scan(c,id,{'request_id':str(uuid4()),'raw':raw('b')}).status_code==409

def test_assignment_gtin_duplicate_quantity_and_incomplete_close(env):
    id,number,_=setup(env)
    with client(env) as unauthorized:
        login(unauthorized,'warehouse');assert unauthorized.get('/api/warehouse/tasks/'+id).status_code==403
        assert scan(unauthorized,id).status_code==403
    with client(env) as c:
        login(c,'tsd')
        assert scan(c,id,{'request_id':str(uuid4()),'raw':raw(gtin='04640638341173')}).status_code==422
        wrong=raw().replace('\x1d3010','\x1d309');assert scan(c,id,{'request_id':str(uuid4()),'raw':wrong}).status_code==422
        saved=scan(c,id);assert saved.status_code==200
        assert scan(c,id).status_code==409
        data=c.get('/api/warehouse/tasks/'+id).json();assert c.post('/api/warehouse/tasks/'+id+'/close',json={'revision':data['task']['revision'],'confirmation':number}).status_code==409
        assert c.delete('/api/warehouse/tasks/'+id+'/scans/'+saved.json()['id']).status_code==200
        assert scan(c,id).status_code==200

def test_concurrent_same_cis_is_counted_once(env):
    id,_,_=setup(env)
    with client(env) as a,client(env) as b:
        login(a,'tsd');login(b,'tsd')
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda c:scan(c,id).status_code,[a,b]))
        assert sorted(results)==[200,409]
        assert a.get('/api/warehouse/tasks/'+id).json()['lines'][0]['scanned']==1

def test_shipments_export_only_after_complete_close_and_reopen_cancel(env):
    id,number,_=setup(env,labels=1,kind='shipment')
    with client(env) as c:
        login(c,'tsd');assert c.get('/api/warehouse/tasks/'+id+'/export').status_code==409
        assert scan(c,id).status_code==200
        data=c.get('/api/warehouse/tasks/'+id).json();assert c.post('/api/warehouse/tasks/'+id+'/close',json={'revision':data['task']['revision'],'confirmation':number}).status_code==200
        response=c.get('/api/warehouse/tasks/'+id+'/export');assert response.status_code==200 and response.content.startswith(b'PK')
    with client(env) as specialist:
        login(specialist,'specialist');data=specialist.get('/api/warehouse/tasks/'+id).json();assert specialist.post('/api/warehouse/tasks/'+id+'/reopen',json={'revision':data['task']['revision'],'confirmation':number,'reason':'Повторная проверка'}).status_code==200
    with client(env) as log:
        login(log,'logistics');data=log.get('/api/warehouse/tasks/'+id+'?logistics=true').json();assert log.post('/api/warehouse/tasks/'+id+'/cancel',json={'revision':data['task']['revision'],'confirmation':number,'reason':'Изменение поставки'}).status_code==200

def test_task_and_invoice_lists_use_creation_time_with_uuid_cursor(env,engine):
    first,_,doc1=setup(env);second,_,doc2=setup(env)
    # Newest record deliberately has the smallest UUID; timestamps determine order.
    first,second=sorted([first,second],reverse=True)
    doc1,doc2=sorted([doc1,doc2],reverse=True)
    now=datetime.now(timezone.utc)
    with engine.begin() as db:
        for table,older,newer in [('bm_documents',doc1,doc2),('warehouse_tasks',first,second)]:
            db.execute(text('UPDATE '+table+' SET created_at=:stamp WHERE id=:id'),{'stamp':now-timedelta(days=1),'id':older})
            db.execute(text('UPDATE '+table+' SET created_at=:stamp WHERE id=:id'),{'stamp':now,'id':newer})
    with client(env) as c:
        login(c,'tsd');url='/api/warehouse/tasks?organization_id='+str(env[1]);data=c.get(url).json();assert [r['id'] for r in data['tasks']]==[second,first]
        assert [r['id'] for r in c.get(url+'&before_id='+second).json()['tasks']]==[first]
    with client(env) as c:
        login(c);url='/api/documents?organization_id='+str(env[1]);assert [r['id'] for r in c.get(url).json()['documents']]==[doc2,doc1]
        assert [r['id'] for r in c.get(url+'&before_id='+doc2).json()['documents']]==[doc1]

import pytest
from sqlalchemy import text
from test_auth_postgres import env,client,login
from test_warehouse_postgres import setup,scan
pytestmark=[pytest.mark.postgres,pytest.mark.usefixtures('fresh_schema')]


@pytest.mark.parametrize('kind',['receipt','shipment'])
def test_admin_archives_complete_invoice_without_losing_evidence(env,engine,kind):
    id,number,source=setup(env,labels=1,kind=kind)
    with client(env) as c:
        login(c,'tsd');assert scan(c,id).status_code==200
        revision=c.get('/api/warehouse/tasks/'+id).json()['task']['revision']
        assert c.post('/api/warehouse/tasks/'+id+'/archive',json={'revision':revision,'confirmation':number}).status_code==403
    with client(env) as c:
        login(c,'administrator');url='/api/warehouse/tasks/'+id
        archived=c.post(url+'/archive',json={'revision':revision,'confirmation':number});assert archived.status_code==200,archived.text
        assert c.post(url+'/archive',json={'revision':revision,'confirmation':number}).status_code==200
        data=c.get(url).json();task=data['task'];assert task['state']=='closed' and task['archived_at'] and len(data['scans'])==1
        query={'organization_id':str(env[1]),'kind':kind,'closed':True}
        assert not c.get('/api/warehouse/tasks',params=query).json()['tasks']
        assert c.get('/api/warehouse/tasks',params={**query,'archived':True}).json()['tasks'][0]['id']==id
        assert c.post(url+'/reopen',json={'revision':task['revision'],'confirmation':number,'reason':'Проверка возврата'}).status_code==409
        assert c.post(url+'/unarchive',json={'revision':task['revision'],'confirmation':number}).status_code==200
        restored=c.get(url).json();assert restored['task']['state']=='closed' and not restored['task']['archived_at'] and len(restored['scans'])==1
    with engine.connect() as c:
        assert c.execute(text('SELECT state FROM bm_documents WHERE id=:id'),{'id':source}).scalar_one()=='prepared'
        assert c.execute(text("SELECT count(*) FROM warehouse_events WHERE task_id=:id AND action='archived'"),{'id':id}).scalar_one()==1


def test_incomplete_and_cancelled_tasks_cannot_be_archived(env):
    id,number,_=setup(env,labels=1)
    with client(env) as c:
        login(c,'administrator');url='/api/warehouse/tasks/'+id;task=c.get(url).json()['task']
        assert c.post(url+'/archive',json={'revision':task['revision'],'confirmation':number}).status_code==409
        assert c.post(url+'/archive',json={'revision':task['revision'],'confirmation':'wrong'}).status_code==422
        assert not c.get(url).json()['task']['archived_at']
        cancelled=c.post(url+'/cancel',json={'revision':task['revision'],'confirmation':number,'reason':'Отменено для проверки'});assert cancelled.status_code==200,cancelled.text
        task=c.get(url).json()['task']
        assert c.post(url+'/archive',json={'revision':task['revision'],'confirmation':number}).status_code==409

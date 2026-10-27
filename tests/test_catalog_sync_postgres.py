"""Mock only the external contract; durable progress uses real PostgreSQL."""
import pytest
from sqlalchemy import text
from test_auth_postgres import env,client,login
from test_business_postgres import connections
from marking_erp.business import store
from marking_erp.business.worker import BusinessWorker,background
from marking_erp.business.nk import Client
from marking_erp.business.errors import RemoteError
from marking_erp.business.catalog_sync import SyncProblem

pytestmark=[pytest.mark.postgres,pytest.mark.usefixtures('fresh_schema')]


def job(env,engine,windows):
    with engine.connect() as c:configured=c.execute(text('SELECT EXISTS(SELECT 1 FROM integration_settings)')).scalar_one()
    if not configured:
        with client(env) as c:
            login(c,'administrator');connections(c,env,nk_key='local-contract-fixture')
    with engine.begin() as c:
        actor=background(c,env[3]['specialist'],env[1])
        return store.create(actor,'sync','Sync fixture',{'from':windows[0][0],'to':windows[-1][1],'windows':windows,'count':0,'offset':0},env[1],state='processing')['id']


def read(engine,id):
    with engine.connect() as c:return store.item(c,id)


def test_413_splits_ranges_and_completes_all_windows(env,engine,monkeypatch):
    id=job(env,engine,[['2026-01-01 00:00:00','2026-01-01 00:00:04',0]])
    seen=[]
    def call(self,method,path,params=None,body=None):
        seen.append(params)
        if params['to_date']=='2026-01-01 00:00:04' and params['from_date']=='2026-01-01 00:00:00':raise RemoteError('too large',status=413)
        return {'goods':[],'total':0}
    monkeypatch.setattr(Client,'call',call);worker=BusinessWorker(engine,env[0])
    worker.sync(id);assert len(read(engine,id)['body']['windows'])==2
    worker.sync(id);assert read(engine,id)['state']=='processing'
    worker.sync(id);assert read(engine,id)['state']=='done' and len(seen)==3
    assert seen[1]['to_date']==seen[2]['from_date']=='2026-01-01 00:00:02'


def test_boundary_duplicates_count_once_and_partial_details_never_complete(env,engine,monkeypatch):
    id=job(env,engine,[['2026-01-01 00:00:00','2026-01-01 00:00:02',0],['2026-01-01 00:00:02','2026-01-01 00:00:04',0]])
    def call(self,method,path,params=None,body=None):
        if path=='/v4/product-list':return {'goods':[{'good_id':n} for n in ([1,2] if params['from_date'].endswith('00:00:00') else [2,3])],'total':2}
        return [{'good_id':int(n)} for n in params['good_ids'].split(';')]
    monkeypatch.setattr(Client,'call',call);worker=BusinessWorker(engine,env[0])
    worker.sync(id);worker.sync(id);row=read(engine,id)
    assert row['state']=='done' and row['body']['count']==3
    with engine.connect() as c:
        assert c.execute(text('SELECT count(*) FROM catalog_sync_entries WHERE item_id=:id'),{'id':id}).scalar_one()==3
        assert c.execute(text("SELECT count(*) FROM business_items WHERE kind='card'")).scalar_one()==3
        assert c.execute(text('SELECT count(*) FROM external_operations')).scalar_one()==0
    bad=job(env,engine,[['2026-01-01 00:00:00','2026-01-01 00:00:02',0]])
    def incomplete(self,method,path,params=None,body=None):
        return {'goods':[{'good_id':1}],'total':1} if path=='/v4/product-list' else []
    monkeypatch.setattr(Client,'call',incomplete)
    with pytest.raises(SyncProblem):worker.sync(bad)
    assert read(engine,bad)['state']!='done' and read(engine,bad)['body']['count']==0


def test_single_second_overflow_pauses_and_resume_keeps_progress(env,engine,monkeypatch):
    id=job(env,engine,[['2026-01-01 00:00:00','2026-01-01 00:00:01',0]])
    def call(self,*args,**kwargs):raise RemoteError('too large',status=413)
    monkeypatch.setattr(Client,'call',call);worker=BusinessWorker(engine,env[0]);worker.tick()
    row=read(engine,id);assert row['state']=='blocked' and 'секундный' in row['body']['error']
    with client(env) as c:
        login(c,'specialist')
        response=c.post('/api/catalog-sync/'+str(id)+'/resume',json={'revision':row['revision']});assert response.status_code==200,response.text
        assert response.json()['body']['windows']==row['body']['windows']
    assert read(engine,id)['state']=='processing'


def test_429_keeps_active_progress_without_false_failure(env,engine,monkeypatch):
    id=job(env,engine,[['2026-01-01 00:00:00','2026-01-01 00:00:04',0]])
    def call(self,*args,**kwargs):raise RemoteError('cooldown',status=429,retry_after=30)
    monkeypatch.setattr(Client,'call',call);worker=BusinessWorker(engine,env[0]);worker.tick()
    assert read(engine,id)['state']=='processing' and read(engine,id)['body']['count']==0


def test_authorized_resume_can_take_over_disabled_initiator(env,engine,monkeypatch):
    with client(env) as c:
        login(c,'administrator');connections(c,env,nk_key='local-contract-fixture')
        id=job(env,engine,[['2026-01-01 00:00:00','2026-01-01 00:00:04',0]])
        worker=BusinessWorker(engine,env[0]);worker.pause_sync(id,'Проверьте подключение')
        with engine.begin() as db:db.execute(text('UPDATE users SET enabled=false WHERE id=:id'),{'id':env[3]['specialist']})
        row=read(engine,id)
        result=c.post('/api/catalog-sync/'+str(id)+'/resume',json={'revision':row['revision']});assert result.status_code==200,result.text
    def call(self,*args,**kwargs):return {'goods':[],'total':0}
    monkeypatch.setattr(Client,'call',call);worker.sync(id)
    assert read(engine,id)['state']=='done'

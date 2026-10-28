"""Mutable rules affect new decisions; prepared tasks retain their evidence."""
import json
from uuid import uuid4
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_auth_postgres import env,client,login
from test_warehouse_postgres import setup,scan,raw
from test_documents_postgres import body as invoice
from marking_erp.business import store
from marking_erp.business.worker import background
from marking_erp.policy import snapshot
pytestmark=[pytest.mark.postgres,pytest.mark.usefixtures('fresh_schema')]


def rules(engine,org,**values):
    with engine.begin() as c:
        c.execute(text('INSERT INTO policy_revisions(scope_key,organization_id,revision) VALUES (:scope,:org,1) ON CONFLICT DO NOTHING'),{'scope':str(org),'org':org})
        for key,value in values.items():c.execute(text('INSERT INTO policy_values(scope_key,rule_id,value) VALUES (:scope,:key,CAST(:value AS jsonb)) ON CONFLICT(scope_key,rule_id) DO UPDATE SET value=EXCLUDED.value'),{'scope':str(org),'key':key,'value':json.dumps(value)})
        c.execute(text('UPDATE policy_revisions SET revision=revision+1 WHERE scope_key=:scope'),{'scope':str(org)})


def test_task_snapshot_duplicate_guidance_and_close_keep_guards(env,engine):
    rules(engine,env[1],**{'warehouse.duplicate.behavior':'error','warehouse.complete.confirm':False,'warehouse.missing.warning_threshold':2})
    id,_,_=setup(env,labels=2)
    rules(engine,env[1],**{'warehouse.duplicate.behavior':'error_with_delete_action','warehouse.complete.confirm':True,'warehouse.missing.warning_threshold':1})
    with client(env) as c:
        login(c,'tsd');path='/api/warehouse/tasks/'+id
        first=c.get(path).json();assert first['rules']['warehouse.complete.confirm'] is False and first['shortage']['warning']
        assert c.post(path+'/close',json={'revision':first['task']['revision']}).status_code==409
        assert scan(c,id).status_code==200
        duplicate=scan(c,id);assert duplicate.status_code==409 and 'scan_id' not in duplicate.json()['detail']
        next=c.get(path).json();assert not next['shortage']['warning'] and next['shortage']['missing']==1
        assert scan(c,id,{'request_id':str(uuid4()),'raw':raw('b')}).status_code==200
        value=c.get(path).json()
        assert c.post(path+'/close',json={'revision':value['task']['revision']}).status_code==200
    other,_,_=setup(env,labels=1)
    with client(env) as c:
        login(c,'tsd');scan(c,other)
        assert 'scan_id' in scan(c,other).json()['detail']
        value=c.get('/api/warehouse/tasks/'+other).json()
        assert c.post('/api/warehouse/tasks/'+other+'/close',json={'revision':value['task']['revision']}).status_code==422


def test_live_display_preferences_default_filter_and_conditional_refresh(env,engine):
    id,number,_=setup(env,labels=1)
    with client(env) as c:
        login(c,'tsd');scan(c,id);task=c.get('/api/warehouse/tasks/'+id).json()
        assert c.post('/api/warehouse/tasks/'+id+'/close',json={'revision':task['task']['revision'],'confirmation':number}).status_code==200
        token=c.get('/api/warehouse/tasks/'+id).json()['sync_token']
        rules(engine,env[1],**{'warehouse.hide_completed':False,'warehouse.devices.poll_seconds':1})
        data=c.get('/api/warehouse/tasks',params={'organization_id':str(env[1])}).json()
        assert data['closed'] and len(data['tasks'])==1
        assert c.get('/api/warehouse/tasks',params={'organization_id':str(env[1]),'closed':'false'}).json()['tasks']==[]
        refreshed=c.get('/api/warehouse/tasks/'+id,params={'sync_token':token}).json()
        assert refreshed['preferences']['warehouse.devices.poll_seconds']==1


def test_transfer_requires_consent_from_source_snapshot_before_creating_task(env,engine):
    with client(env) as c:
        login(c);value=invoice(env);source=c.post('/api/documents',json=value).json()['id']
        assert c.post('/api/documents/'+source+'/prepare',json={'revision':1}).status_code==200
    rules(engine,env[1],**{'logistics.transfer.confirm':False})
    with client(env) as c:
        login(c,'logistics');body={'request_id':str(uuid4()),'source_id':source,'source_revision':2,'kind':'receipt','assignees':[str(env[3]['tsd'])]}
        assert c.post('/api/logistics/warehouse-transfer',json=body).status_code==422
        assert c.post('/api/logistics/warehouse-transfer',json={**body,'confirmed':True}).status_code==200
        assert c.post('/api/logistics/warehouse-transfer',json=body).json()['replayed']
    with client(env) as c:
        login(c);other=c.post('/api/documents',json=invoice(env,number='NEW-NO-CONFIRM')).json()['id']
        assert c.post('/api/documents/'+other+'/prepare',json={'revision':1}).status_code==200
    with client(env) as c:
        login(c,'logistics');assert c.post('/api/logistics/warehouse-transfer',json={**body,'request_id':str(uuid4()),'source_id':other}).status_code==200


@pytest.mark.parametrize('extra',[True,False])
def test_split_additional_ack_never_replaces_factual_application(env,engine,extra):
    with engine.begin() as c:
        actor=background(c,env[3]['specialist'],env[1]);policy=snapshot(c,env[1]);policy['values']['split.application.extra_confirm']=extra
        row=store.create(actor,'split','Split-rule',{'policy':policy,'pn':'PN'},env[1],state='labels_ready')
    rules(engine,env[1],**{'split.application.extra_confirm':not extra})
    with client(env) as c:
        login(c,'specialist');path='/api/business/'+str(row['id'])+'/applied';body={'revision':1}
        assert c.post(path,json={**body,'extra_confirmed':True}).status_code==422
        result=c.post(path,json={**body,'confirmed':True})
        if extra:
            assert result.status_code==422
            result=c.post(path,json={**body,'confirmed':True,'extra_confirmed':True})
        assert result.status_code==200,result.text
        assert result.json()['state']=='application_confirmed'


def test_archive_retention_is_append_only_and_cannot_be_shortened(env,engine):
    rules(engine,env[1],**{'archive.retention_months':24})
    with client(env) as c:
        login(c,'administrator');id=c.post('/api/documents',json=invoice(env)).json()['id']
        assert c.post('/api/documents/'+id+'/archive',json={'revision':1,'confirmation':'INV-01','reason':'Document not required for this supply'}).status_code==200
        first=c.get('/api/documents/'+id).json()['archive_retain_until'];assert first
        assert c.post('/api/documents/'+id+'/restore',json={'revision':2,'confirmation':'INV-01','reason':'Return document for later processing'}).status_code==200
        rules(engine,env[1],**{'archive.retention_months':6})
        assert c.post('/api/documents/'+id+'/archive',json={'revision':3,'confirmation':'INV-01','reason':'Document archived after repeat review'}).status_code==200
        assert c.get('/api/documents/'+id).json()['archive_retain_until']==first
    with engine.connect() as c:assert c.execute(text('SELECT count(*) FROM archive_retention')).scalar_one()==2
    with pytest.raises(DBAPIError),engine.begin() as c:c.execute(text('DELETE FROM archive_retention'))


def test_strict_owner_cannot_be_bypassed_by_legacy_connection_flag(env,engine,monkeypatch):
    from test_warehouse_check_postgres import enable,task,check,fake_info
    from marking_erp.business.live import Live
    enable(env);id,_,_=task(env,codes=[raw('a')]);calls=[]
    original=fake_info(calls)
    def foreign(self,cises,**kw):
        values=original(self,cises,**kw)
        for info in values.values():info['ownerInn']='7800000000'
        return values
    monkeypatch.setattr(Live,'info',foreign)
    with engine.begin() as db:db.execute(text("UPDATE integration_settings SET public=jsonb_set(public,'{ignore_legal_entity}','true') WHERE organization_id=:org"),{'org':env[1]})
    with client(env) as c:
        login(c,'tsd');assert scan(c,id).status_code==200
        result,_=check(c,id);assert not result['ok'] and 'юридическому' in result['message']
    with client(env) as c:
        import pyotp,time
        from test_auth_postgres import TOTP_SECRET
        login(c,'administrator',otp=pyotp.TOTP(TOTP_SECRET).at(time.time()+30))
        assert c.put('/api/connections',json={'organization_id':str(env[1]),'revision':1,'environment':'production','ignore_legal_entity':True}).status_code==422

"""Conditional warehouse reads authorize every call and never perform a check."""
from datetime import datetime,timedelta,timezone
import json
import pytest
from sqlalchemy import text
from test_auth_postgres import env,client,login
from test_warehouse_postgres import setup,scan
pytestmark=[pytest.mark.postgres,pytest.mark.usefixtures('fresh_schema')]


def test_unchanged_is_small_and_scan_close_source_change_invalidate(env,engine):
    id,number,source=setup(env,labels=1)
    with client(env) as c:
        login(c,'tsd');path='/api/warehouse/tasks/'+id
        first=c.get(path).json();token=first['sync_token']
        unchanged=c.get(path,params={'sync_token':token})
        assert unchanged.json()=={'unchanged':True,'sync_token':token}
        assert len(unchanged.content)<150
        assert scan(c,id).status_code==200
        next=c.get(path,params={'sync_token':token}).json()
        assert next['sync_token']!=token and next['lines'][0]['scanned']==1
        assert c.post(path+'/close',json={'revision':next['task']['revision'],'confirmation':number}).status_code==200
        closed=c.get(path,params={'sync_token':next['sync_token']}).json();assert closed['task']['state']=='closed'
        with engine.begin() as db:db.execute(text("UPDATE bm_documents SET state='archived' WHERE id=:id"),{'id':source})
        paused=c.get(path,params={'sync_token':closed['sync_token']}).json();assert paused['paused']
        assert c.get(path,params={'sync_token':'x'*65}).status_code==422
    with engine.connect() as db:assert db.execute(text('SELECT count(*) FROM external_operations')).scalar_one()==0


def test_assignment_and_permission_changes_are_live_even_with_known_token(env,engine):
    id,_,_=setup(env)
    with client(env) as c:
        login(c,'tsd');path='/api/warehouse/tasks/'+id
        first=c.get(path).json()
        with engine.begin() as db:db.execute(text("UPDATE group_grants SET scope='none' WHERE group_slug='tsd' AND permission_id='warehouse.receipt.scan'"))
        updated=c.get(path,params={'sync_token':first['sync_token']}).json()
        assert 'warehouse.receipt.scan' not in updated['permissions'] and updated['sync_token']!=first['sync_token']
        with engine.begin() as db:db.execute(text('DELETE FROM operation_assignments WHERE target_id=:id AND user_id=:user'),{'id':id,'user':env[3]['tsd']})
        denied=c.get(path,params={'sync_token':updated['sync_token']})
        assert denied.status_code==403 and 'sync_token' not in denied.json()


def test_check_expiry_and_policy_change_invalidate_without_writes(env,engine,monkeypatch):
    id,_,_=setup(env)
    with engine.begin() as db:
        result={'ok':True,'checked_at':(datetime.now(timezone.utc)-timedelta(seconds=60)).isoformat()}
        db.execute(text('UPDATE warehouse_tasks SET check_result=CAST(:result AS jsonb) WHERE id=:id'),{'id':id,'result':json.dumps(result)})
        db.execute(text("INSERT INTO policy_revisions(scope_key,organization_id,revision) VALUES (:scope,:org,1) ON CONFLICT DO NOTHING"),{'scope':str(env[1]),'org':env[1]})
        db.execute(text("INSERT INTO policy_values(scope_key,rule_id,value) VALUES (:scope,'warehouse.external_fact_max_age_seconds','300')"),{'scope':str(env[1])})
    with client(env) as c:
        login(c,'tsd');path='/api/warehouse/tasks/'+id;first=c.get(path).json()
        import marking_erp.warehouse_routes as routes
        original=routes.datetime
        class Later(original):
            @classmethod
            def now(cls,tz=None):return original.now(tz)+timedelta(seconds=301)
        with monkeypatch.context() as clock:
            clock.setattr(routes,'datetime',Later)
            assert c.get(path,params={'sync_token':first['sync_token']}).json()['sync_token']!=first['sync_token']
        with engine.begin() as db:db.execute(text("UPDATE policy_values SET value='30' WHERE scope_key=:scope AND rule_id='warehouse.external_fact_max_age_seconds'"),{'scope':str(env[1])})
        expired=c.get(path,params={'sync_token':first['sync_token']}).json()
        assert expired['sync_token']!=first['sync_token'] and not expired['check_fresh']
    with engine.connect() as db:
        assert db.execute(text('SELECT count(*) FROM warehouse_events WHERE action=:action'),{'action':'checked'}).scalar_one()==0
        assert db.execute(text('SELECT count(*) FROM external_operations')).scalar_one()==0

from concurrent.futures import ThreadPoolExecutor
import json
from uuid import uuid4
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_auth_postgres import env,client,login
from marking_erp.policy import snapshot

pytestmark=[pytest.mark.postgres,pytest.mark.usefixtures('fresh_schema')]

@pytest.fixture
def policy_env(env,engine):
    with engine.begin() as c:c.execute(text('INSERT INTO user_organizations VALUES (:u,:o)'),{'u':env[3]['administrator'],'o':env[2]})
    return env


def group_body(c,slug='bm'):
    data=c.get('/api/groups').json();g=next(x for x in data['groups'] if x['slug']==slug)
    return {'revision':g['revision'],'grants':dict(g['grants']),'reason':'Проверка политики доступа'}


def rule_body(c,key='system',changes=None):
    data=c.get('/api/business-rules/'+key).json()
    return {'revision':data['revision'],'system_revision':data['system_revision'],'changes':changes or {'nk.sync.interval_seconds':1800},'reason':'Проверка новой политики'}


def test_admin_api_requires_login_permission_fresh_mfa_and_global_scope(env,engine):
    with client(env) as c:
        assert c.get('/api/groups').status_code==401
        login(c,'specialist')
        assert c.get('/api/groups').status_code==403
        assert c.get('/api/business-rules/'+str(env[1])).status_code==403
    with client(env) as c:
        login(c,'administrator');body=group_body(c);body['grants']['nk.cards.publish']='none'
        assert c.put('/api/groups/bm',json=body).status_code==403
        assert c.get('/api/business-rules/system').status_code==403
        assert c.get('/api/business-rules/'+str(env[2])).status_code==403
        with engine.begin() as db:db.execute(text("UPDATE sessions SET elevated_at=now()-interval '6 minutes'"))
        assert c.get('/api/groups').json()['detail']['code']=='reauth_required'


def test_preview_save_revoke_revision_history_and_seed_preservation(policy_env,engine):
    with client(policy_env) as admin,client(policy_env) as employee:
        login(admin,'administrator');login(employee)
        body=group_body(admin);body['grants']['nk.cards.publish']='none'
        assert admin.post('/api/groups/bm/preview',json=body).status_code==200
        assert employee.get('/api/session').status_code==200
        result=admin.put('/api/groups/bm',json=body);assert result.status_code==200,result.text
        assert result.json()['revision']==2
        assert employee.get('/api/session').status_code==401
        assert admin.put('/api/groups/bm',json=body).status_code==409
        h=admin.get('/api/configuration/history?kind=group&entity=bm').json()['history'];assert len(h)==1
        assert h[0]['before_value']['nk.cards.publish']=='own' and h[0]['after_value']['nk.cards.publish']=='none'
        with engine.begin() as c:
            from marking_erp.seed import seed_defaults
            assert seed_defaults(c) is False
            assert c.execute(text("SELECT scope FROM group_grants WHERE group_slug='bm' AND permission_id='nk.cards.publish'")).scalar()=='none'
        for sql in ['DELETE FROM configuration_changes','UPDATE configuration_changes SET reason=\'changed history\'','TRUNCATE configuration_changes']:
            with pytest.raises(DBAPIError),engine.begin() as c:c.execute(text(sql))


def test_group_invariants_dependencies_mfa_tsd_and_admin_lockout(policy_env,engine):
    with client(policy_env) as c:
        login(c,'administrator')
        body=group_body(c);body['grants']['nk.cards.view']='none'
        assert c.put('/api/groups/bm',json=body).status_code==422
        body=group_body(c);body['grants']['users.manage']='all'
        assert c.put('/api/groups/bm',json=body).status_code==422
        body=group_body(c,'specialist');body['grants']['users.manage']='all'
        assert c.put('/api/groups/specialist',json=body).status_code==422
        body=group_body(c,'administrator');body['grants']['permissions.manage']='none'
        assert c.put('/api/groups/administrator',json=body).status_code==422
        body=group_body(c,'tsd');body['grants']['nk.cards.view']='all'
        assert c.post('/api/groups/tsd/preview',json=body).status_code==422
        body['tsd_confirmation']='РАСШИРИТЬ ТСД'
        assert c.post('/api/groups/tsd/preview',json=body).json()['tsd_expansion'] is True
        assert c.put('/api/groups/tsd',json=body).status_code==200
    with pytest.raises(DBAPIError),engine.begin() as c:c.execute(text("UPDATE group_grants SET scope='none' WHERE group_slug='administrator' AND permission_id='permissions.manage'"))


def test_rule_inheritance_snapshots_reset_and_optimistic_conflict(policy_env,engine):
    org=str(policy_env[1])
    with client(policy_env) as c:
        login(c,'administrator')
        body=rule_body(c);assert c.post('/api/business-rules/system/preview',json=body).status_code==200
        assert c.put('/api/business-rules/system',json=body).status_code==200
        assert c.put('/api/business-rules/system',json=body).status_code==409
        body=rule_body(c,org,{'nk.sync.interval_seconds':2400})
        assert c.put('/api/business-rules/'+org,json=body).status_code==200
        with engine.connect() as db:old=snapshot(db,policy_env[1])
        assert old['values']['nk.sync.interval_seconds']==2400 and old['versions']=={'system':2,'organization':2}
        reset=rule_body(c,org,{'nk.sync.interval_seconds':None});assert c.put('/api/business-rules/'+org,json=reset).status_code==200
        with engine.connect() as db:new=snapshot(db,policy_env[1])
        assert new['values']['nk.sync.interval_seconds']==1800 and old['values']['nk.sync.interval_seconds']==2400
        stale=rule_body(c,org,{'nk.sync.interval_seconds':3000})
        assert c.put('/api/business-rules/system',json=rule_body(c,changes={'nk.sync.interval_seconds':3600})).status_code==200
        assert c.put('/api/business-rules/'+org,json=stale).status_code==409
        history=c.get('/api/configuration/history?kind=rules&entity='+org).json()['history'];assert len(history)==2
        assert c.get('/api/configuration/history?kind=rules&entity='+org+'&before_id='+str(history[-1]['id'])).json()['history']==[]


@pytest.mark.parametrize('changes',[{'archive.retention_months':5},{'nk.sync.interval_seconds':True},{'workflow.stop.confirm_number':False},{'warehouse.status_profile':'approved_stricter'},{'warehouse.owner.policy':'approved_exception'},{'labels.printer_profile':'other'},{'unknown.rule':1},{'nk.duplicates.similarity':'0.9'}])
def test_invalid_or_unsafe_business_rules_are_rejected(policy_env,changes):
    with client(policy_env) as c:
        login(c,'administrator');body=rule_body(c,changes=changes)
        assert c.put('/api/business-rules/system',json=body).status_code==422
        assert c.get('/api/business-rules/system').json()['revision']==1


def test_two_concurrent_saves_have_one_winner(policy_env):
    with client(policy_env) as c:
        login(c,'administrator');body=rule_body(c)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:c.put('/api/business-rules/system',json=body).status_code,range(2)))
        assert sorted(results)==[200,409]


def test_context_simulation_does_not_cross_organizations(policy_env):
    with client(policy_env) as c:
        login(c,'administrator');body={'user_id':str(policy_env[3]['bm']),'permission':'marking.documents.edit','organization_id':str(policy_env[1]),'owner_id':str(policy_env[3]['bm'])}
        result=c.post('/api/groups/check-access',json=body);assert result.status_code==200 and result.json()['allowed'] is True
        body['owner_id']=str(uuid4());assert c.post('/api/groups/check-access',json=body).json()['allowed'] is False
        body['organization_id']=str(policy_env[2]);assert c.post('/api/groups/check-access',json=body).json()['allowed'] is False


def test_server_checks_read_dependency_after_direct_database_revoke(policy_env,engine):
    from marking_erp import auth
    from datetime import datetime,timezone
    with engine.begin() as c:
        user=dict(c.execute(text("SELECT * FROM users WHERE group_slug='bm'")).mappings().one())
        c.execute(text("UPDATE group_grants SET scope='none' WHERE group_slug='bm' AND permission_id='nk.cards.view'"))
        actor=auth.Actor(c,user,'synthetic',datetime.now(timezone.utc))
        with pytest.raises(Exception) as error:actor.require('nk.cards.publish',organization_id=policy_env[1],owner_id=user['id'])
        assert getattr(error.value,'status_code',None)==403

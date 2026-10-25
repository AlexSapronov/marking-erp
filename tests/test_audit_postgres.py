import json
from datetime import datetime, timezone
from uuid import uuid4
import pytest
from sqlalchemy import text
from marking_erp import auth
from test_auth_postgres import env, client, login

pytestmark=[pytest.mark.postgres,pytest.mark.usefixtures('fresh_schema')]


@pytest.fixture
def global_env(env,engine):
    with engine.begin() as c:
        c.execute(text('INSERT INTO user_organizations VALUES (:user,:org)'),{'user':env[3]['administrator'],'org':env[2]})
    return env


def test_global_audit_requires_authentication_permission_and_all_organizations(env):
    with client(env) as c:assert c.get('/api/audit').status_code==401
    for group in ['bm','logistics','warehouse','tsd','specialist','administrator']:
        with client(env) as c:
            login(c,group)
            assert c.get('/api/audit').status_code==403


def test_audit_projection_filtering_and_read_only(global_env,engine):
    actor=global_env[3]['warehouse'];entity=uuid4()
    with client(global_env) as c:
        login(c,'administrator')
        with engine.begin() as db:
            auth.audit(db,actor,'user.created',entity,{'password':'private-key','raw_km':'010123-secret'})
            auth.audit(db,None,'login.failed','private-throttle-key')
            auth.audit(db,None,'new.private-key','private-entity',{'secret':'private-key'})
            count=db.execute(text('SELECT count(*) FROM audit_events')).scalar_one()
        response=c.get('/api/audit');assert response.status_code==200
        assert response.headers['cache-control']=='no-store'
        assert 'private-' not in response.text and '010123' not in response.text
        assert 'detail' not in response.json()['events'][0]
        assert response.json()['events'][0]['action']=='unknown'
        assert response.json()['events'][1]['entity'] is None
        rows=c.get('/api/audit',params={'action':'user.created','actor_id':str(actor)}).json()['events']
        assert len(rows)==1 and rows[0]['entity']==str(entity)
        assert c.get('/api/audit',params={'actor_id':str(uuid4())}).json()['events']==[]
        with engine.connect() as db:assert db.execute(text('SELECT count(*) FROM audit_events')).scalar_one()==count


def test_keyset_pagination_excludes_newer_insertions(global_env,engine):
    with client(global_env) as c:
        login(c,'administrator')
        with engine.begin() as db:
            for _ in range(60):auth.audit(db,None,'system.restored','backup')
        params={'action':'system.restored'}
        first=c.get('/api/audit',params=params).json();assert len(first['events'])==25
        with engine.begin() as db:auth.audit(db,None,'system.restored','backup')
        second=c.get('/api/audit',params={**params,'before_id':first['next_before_id']}).json()
        third=c.get('/api/audit',params={**params,'before_id':second['next_before_id']}).json()
        ids=[int(row['id']) for page in [first,second,third] for row in page['events']]
        assert len(ids)==len(set(ids))==60 and ids==sorted(ids,reverse=True)
        assert third['next_before_id'] is None


def test_invalid_filters_and_period_boundaries(global_env,engine):
    with client(global_env) as c:
        login(c,'administrator')
        for params in [{'action':'unknown'}, {'actor_id':'bad'}, {'before_id':0}, {'before_id':2**63},
                       {'since':'2026-10-01T00:00:00'},
                       {'since':'2026-10-02T00:00:00Z','until':'2026-10-01T00:00:00Z'}]:
            assert c.get('/api/audit',params=params).status_code==422
        stamp=datetime(2026,1,1,tzinfo=timezone.utc)
        with engine.begin() as db:
            db.execute(text("INSERT INTO audit_events(action,entity,created_at) VALUES ('system.restored','backup',:stamp)"),{'stamp':stamp})
        result=c.get('/api/audit',params={'since':stamp.isoformat(),'until':stamp.isoformat()}).json()
        assert len(result['events'])==1 and result['events'][0]['action']=='system.restored'
        assert c.get('/api/audit',params={'since':'2099-01-01T00:00:00Z'}).json()['events']==[]


def test_existing_session_loses_audit_on_mfa_expiry_revoke_and_scope_loss(global_env,engine):
    with client(global_env) as c:
        login(c,'administrator');assert c.get('/api/audit').status_code==200
        with engine.begin() as db:db.execute(text("UPDATE sessions SET elevated_at=now()-interval '6 minutes'"))
        assert c.get('/api/audit').json()['detail']['code']=='reauth_required'
        with engine.begin() as db:
            db.execute(text('UPDATE sessions SET elevated_at=now()'))
            db.execute(text("UPDATE group_grants SET scope='none' WHERE group_slug='administrator' AND permission_id='audit.view'"))
        assert c.get('/api/audit').status_code==403
        with engine.begin() as db:
            db.execute(text("UPDATE group_grants SET scope='all' WHERE group_slug='administrator' AND permission_id='audit.view'"))
            db.execute(text('DELETE FROM user_organizations WHERE user_id=:user AND organization_id=:org'),{'user':global_env[3]['administrator'],'org':global_env[2]})
        assert c.get('/api/audit').status_code==403

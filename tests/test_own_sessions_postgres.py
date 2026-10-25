from datetime import datetime, timedelta, timezone
import pytest
from sqlalchemy import text
from marking_erp import auth
from test_auth_postgres import env, client, login

pytestmark=[pytest.mark.postgres,pytest.mark.usefixtures('fresh_schema')]


def extra_session(engine,user,*,version=1,expired=False):
    token=auth.secrets.token_urlsafe(32)
    with engine.begin() as db:
        db.execute(text('''INSERT INTO sessions(token_hash,user_id,auth_version,created_at,expires_at)
            VALUES (:hash,:user,:version,:created,:expires)'''),
            {'hash':auth.digest(token),'user':user,'version':version,
             'created':datetime.now(timezone.utc)-timedelta(hours=2),
             'expires':datetime.now(timezone.utc)+timedelta(hours=-1 if expired else 1)})
    return token


@pytest.mark.parametrize('group',['bm','logistics','warehouse','tsd','specialist','administrator'])
def test_all_groups_can_only_list_their_valid_sessions(env,engine,group):
    user=env[3][group]
    tokens=[extra_session(engine,user),extra_session(engine,user),extra_session(engine,user,expired=True),extra_session(engine,user,version=0)]
    tokens.append(extra_session(engine,env[3]['warehouse' if group!='warehouse' else 'bm']))
    with client(env) as c:
        login(c,group)
        with engine.connect() as db:before=db.execute(text('SELECT count(*) FROM audit_events')).scalar_one()
        response=c.get('/api/session/sessions');assert response.status_code==200
        data=response.json();assert data['total']==3 and len(data['sessions'])==3
        assert data['sessions'][0]['is_current'] is True
        assert sum(row['is_current'] for row in data['sessions'])==1
        assert all(set(row)=={'created_at','expires_at','is_current'} for row in data['sessions'])
        assert response.headers['cache-control']=='no-store'
        for token in [*tokens,c.cookies.get(env[0].cookie_name)]:
            assert token not in response.text and auth.digest(token) not in response.text
        for forbidden in ['csrf','password','mfa','user_id','auth_version']:assert forbidden not in response.text
        with engine.connect() as db:assert db.execute(text('SELECT count(*) FROM audit_events')).scalar_one()==before


def test_logout_others_preserves_current_cookie_csrf_and_other_users(env,engine):
    with client(env) as current,client(env) as other,client(env) as foreign:
        initial=login(current);login(other);login(foreign,'warehouse')
        token=current.cookies.get(env[0].cookie_name);csrf=initial.json()['csrf']
        with engine.connect() as db:version=db.execute(text('SELECT auth_version FROM users WHERE id=:id'),{'id':env[3]['bm']}).scalar_one()
        for _ in range(2):
            assert current.post('/api/session/logout-others').status_code==200
            assert current.cookies.get(env[0].cookie_name)==token
            assert current.get('/api/session').json()['csrf']==csrf
            assert other.get('/api/session').status_code==401
            assert foreign.get('/api/session').status_code==200
            assert current.get('/api/session/sessions').json()['total']==1
        with engine.connect() as db:
            assert db.execute(text('SELECT auth_version FROM users WHERE id=:id'),{'id':env[3]['bm']}).scalar_one()==version
            events=db.execute(text("SELECT actor_id,entity FROM audit_events WHERE action='sessions.revoked_others'")).all()
            assert len(events)==2 and all(row[0]==env[3]['bm'] and row[1]==str(env[3]['bm']) for row in events)


def test_sessions_require_session_and_mutation_requires_csrf(env,engine):
    with client(env) as c:
        assert c.get('/api/session/sessions').status_code==401
        assert c.post('/api/session/logout-others').status_code==401
        login(c)
        extra_session(engine,env[3]['bm'])
        c.headers.pop('X-CSRF-Token')
        assert c.post('/api/session/logout-others').status_code==403
        assert c.get('/api/session/sessions').json()['total']==2
        assert c.get('/api/session/logout-others').status_code==405


def test_list_is_bounded_and_always_includes_current_session(env,engine):
    for _ in range(25):extra_session(engine,env[3]['bm'])
    with client(env) as c:
        login(c)
        data=c.get('/api/session/sessions').json()
        assert data['total']==26 and len(data['sessions'])==20
        assert data['sessions'][0]['is_current']
        assert sum(row['is_current'] for row in data['sessions'])==1
        assert c.post('/api/session/logout-others').status_code==200
        assert c.get('/api/session/sessions').json()['total']==1


def test_own_session_cleanup_does_not_require_admin_elevation(env,engine):
    with client(env) as c:
        login(c,'administrator');extra_session(engine,env[3]['administrator'])
        with engine.begin() as db:db.execute(text("UPDATE sessions SET elevated_at=now()-interval '6 minutes'"))
        assert c.get('/api/session/sessions').json()['total']==2
        assert c.post('/api/session/logout-others').status_code==200
        assert c.get('/api/session').status_code==200
        assert c.get('/api/session/sessions').json()['total']==1

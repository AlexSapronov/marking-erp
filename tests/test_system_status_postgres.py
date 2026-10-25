import json
import pytest
from sqlalchemy import text
from marking_erp import system_routes
from test_auth_postgres import env, client, login

pytestmark = [pytest.mark.postgres, pytest.mark.usefixtures('fresh_schema')]


@pytest.fixture
def global_env(env, engine):
    with engine.begin() as c:
        c.execute(text('INSERT INTO user_organizations VALUES (:user,:org)'), {'user': env[3]['administrator'], 'org': env[2]})
    return env


def test_unauthorized_requests_never_run_global_checks(env, monkeypatch):
    monkeypatch.setattr(system_routes, 'inspect_runtime', lambda *_: pytest.fail('not authorized'))
    with client(env) as c:
        assert c.get('/api/system/status').status_code == 401
    for group in ['bm', 'logistics', 'warehouse', 'tsd', 'specialist']:
        with client(env) as c:
            login(c, group)
            assert c.get('/api/system/status').status_code == 403


def test_missing_global_org_scope_denies_before_inspection(env, monkeypatch):
    monkeypatch.setattr(system_routes, 'inspect_runtime', lambda *_: pytest.fail('not authorized'))
    with client(env) as c:
        login(c, 'administrator')
        response = c.get('/api/system/status')
        assert response.status_code == 403 and 'всем действующим организациям' in response.json()['detail']


def test_real_read_only_checks_are_safe_and_do_not_claim_business_readiness(global_env, engine):
    with client(global_env) as c:
        login(c, 'administrator')
        with engine.connect() as db:
            count = db.execute(text('SELECT count(*) FROM audit_events')).scalar_one()
        response = c.get('/api/system/status')
        assert response.status_code == 200
        data = response.json()
        assert data['checks']['schema_revision'] and data['checks']['mfa_decrypt']
        assert not data['checks']['database_runtime_role']  # The fixture connection is the migration owner.
        assert not data['technical_ready'] and not data['business_integrations_verified']
        assert response.headers['cache-control'] == 'no-store'
        body = json.dumps(data)
        for forbidden in ['postgresql', 'JBSWY', '/var/lib', str(global_env[3]['administrator']), 'password_hash']:
            assert forbidden not in body
        with engine.connect() as db:
            assert db.execute(text('SELECT count(*) FROM audit_events')).scalar_one() == count


def test_expired_elevation_and_permission_revocation_block_existing_session(global_env, engine, monkeypatch):
    with client(global_env) as c:
        login(c, 'administrator')
        monkeypatch.setattr(system_routes, 'inspect_runtime', lambda *_: pytest.fail('not authorized'))
        with engine.begin() as db:db.execute(text("UPDATE sessions SET elevated_at=now()-interval '6 minutes'"))
        assert c.get('/api/system/status').json()['detail']['code'] == 'reauth_required'
        with engine.begin() as db:
            db.execute(text('UPDATE sessions SET elevated_at=now()'))
            db.execute(text("UPDATE group_grants SET scope='none' WHERE group_slug='administrator' AND permission_id='system.diagnostics.view'"))
        assert c.get('/api/system/status').status_code == 403


def test_mfa_key_mismatch_is_a_failed_check_without_exposing_key(global_env):
    from cryptography.fernet import Fernet
    from pydantic import SecretStr
    with client(global_env) as c:
        login(c, 'administrator')
        key = Fernet.generate_key().decode()
        global_env[0].mfa_encryption_key = SecretStr(key)
        response = c.get('/api/system/status')
        assert response.status_code == 200 and response.json()['checks']['mfa_decrypt'] is False
        assert not response.json()['technical_ready'] and key not in response.text

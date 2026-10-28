import json
import pytest
from sqlalchemy import text
from marking_erp import system_routes
from test_auth_postgres import env, client, login
from test_operations_postgres import queue_env

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


def test_operational_evidence_is_local_redacted_and_does_not_claim_pilot(global_env, engine, monkeypatch):
    from cryptography.fernet import Fernet
    from uuid import uuid4
    from hashlib import sha256
    import httpx
    settings, org = global_env[:2]
    private = 'PRIVATE_INTEGRATION_TOKEN'
    device = uuid4()
    with engine.begin() as db:
        db.execute(text('INSERT INTO integration_settings(organization_id,public,secret) VALUES (:org,CAST(:public AS jsonb),:secret)'),
            {'org': org, 'public': json.dumps({'environment':'production','writes_enabled':True,'auto_refresh_tokens':True,
                'warehouse_live_checks':False,'private_unlisted_key':private}),
             'secret': Fernet(settings.mfa_encryption_key.get_secret_value().encode()).encrypt(json.dumps({'nk_key':private,'true_token':private,'oms_token':private}).encode())})
        db.execute(text('''INSERT INTO signer_devices(id,organization_id,name,token_hash,certificate_sha256,allowed_kinds,last_seen_at)
            VALUES (:id,:org,:name,:hash,:cert,ARRAY['auth.true','auth.oms'],now())'''),
            {'id':device,'org':org,'name':private,'hash':sha256(private.encode()).hexdigest(),'cert':'a'*64})
        db.execute(text('INSERT INTO signer_verifiers VALUES (:id,true)'), {'id':device})
        db.execute(text('''INSERT INTO integration_auth_state(organization_id,service,renewed_at,next_refresh_at,error)
            VALUES (:org,'true',now(),now(),:error)'''), {'org':org,'error':private})
    # Patch after login: even a diagnostic failure must not trigger an outbound probe.
    with client(global_env) as c:
        login(c,'administrator')
        monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', lambda *_a, **_kw: pytest.fail('No external requests'))
        response = c.get('/api/system/status')
        assert response.status_code == 200, response.text
        data = response.json()['operational']
        item = next(o for o in data['organizations'] if o['organization_id']==str(org))
        assert item['credentials']==dict.fromkeys(('nk_key','true_token','oms_token'),True)
        assert item['signers']=={'registered':1,'trusted':1,'recent':1,'token_capable':1}
        assert item['writes_enabled'] and not item['warehouse_live_checks']
        assert item['token_renewal'][0]['needs_attention'] is True
        assert not data['external_probe_performed'] and not data['hardware_verified']
        assert data['worker_liveness_verified']==data['workers']['available']
        for value in [private, 'a'*64, sha256(private.encode()).hexdigest(), str(device)]:
            assert value not in response.text


def test_credentials_require_integration_permission_and_corruption_is_safe(global_env, engine):
    org = global_env[1]
    with engine.begin() as db:
        db.execute(text("INSERT INTO integration_settings(organization_id,public,secret) VALUES (:org,'{}','broken encrypted secret'::bytea)"), {'org':org})
    with client(global_env) as c:
        login(c,'administrator')
        response=c.get('/api/system/status')
        assert response.status_code==200
        item=next(o for o in response.json()['operational']['organizations'] if o['organization_id']==str(org))
        assert item['credential_state']=='unreadable' and item['credentials'] is None
        with engine.begin() as db:
            db.execute(text("UPDATE group_grants SET scope='none' WHERE group_slug='administrator' AND permission_id='integrations.manage'"))
        response=c.get('/api/system/status')
        assert response.status_code==200
        item=next(o for o in response.json()['operational']['organizations'] if o['organization_id']==str(org))
        assert item['credential_state']=='restricted' and item['credentials'] is None
        assert 'broken encrypted secret' not in response.text


def test_unknown_operations_are_reported_without_retry_or_evidence_leak(queue_env, engine):
    from test_operations_postgres import prepare
    env, _ = queue_env
    with engine.begin() as db:
        db.execute(text('INSERT INTO user_organizations VALUES (:user,:org)'), {'user':env[3]['administrator'],'org':env[2]})
        op=prepare(db,queue_env)
        db.execute(text("UPDATE external_operations SET state='unknown',reason='PRIVATE_REMOTE_RESPONSE' WHERE id=:id"),{'id':op['id']})
    with client(env) as c:
        login(c,'administrator')
        response=c.get('/api/system/status')
        assert response.status_code==200
        rows=response.json()['operational']['operations']
        assert len(rows)==1 and rows[0]['state']=='unknown' and rows[0]['count']==1
        assert 'PRIVATE_REMOTE_RESPONSE' not in response.text and str(op['id']) not in response.text
    with engine.connect() as db:
        assert db.execute(text('SELECT state FROM external_operations WHERE id=:id'),{'id':op['id']}).scalar_one()=='unknown'
        assert db.execute(text('SELECT count(*) FROM operation_attempts')).scalar_one()==0


def test_schema_mismatch_keeps_diagnostics_available_without_querying_business_tables(global_env, monkeypatch):
    monkeypatch.setattr(system_routes,'inspect_runtime',lambda *_: {'ready':False,'checks':{'schema_revision':False}})
    monkeypatch.setattr(system_routes,'operational_status',lambda *_a,**_kw:pytest.fail('Do not query incompatible schema'))
    with client(global_env) as c:
        login(c,'administrator')
        response=c.get('/api/system/status')
        assert response.status_code==200
        assert response.json()['operational'] is None and response.json()['checks']['schema_revision'] is False

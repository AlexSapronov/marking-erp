from uuid import uuid4
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr
from marking_erp.runtime_access import apply_runtime_grants, runtime_profile_matches
from marking_erp.service import inspect_runtime
from marking_erp.web import create_app
from test_auth_postgres import env, login

pytestmark = pytest.mark.postgres


@pytest.fixture
def runtime(engine, fresh_schema, env, tmp_path):
    role = 'runtime_' + uuid4().hex
    password = uuid4().hex
    with engine.begin() as c:
        schema = c.execute(text('SELECT current_schema()')).scalar_one()
        c.exec_driver_sql(f'CREATE ROLE "{role}" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD \'{password}\'')
        apply_runtime_grants(c, role, schema)
    runtime_engine = create_engine(engine.url.set(username=role, password=password), hide_parameters=True,
                                  connect_args={'options': '-c search_path=' + schema})
    settings = env[0].model_copy(update={'file_storage_path': tmp_path / 'files', 'build_sha': 'b' * 40})
    settings.file_storage_path.mkdir(mode=0o700)
    try:
        yield role, schema, runtime_engine, settings
    finally:
        runtime_engine.dispose()
        with engine.begin() as c:
            c.exec_driver_sql(f'DROP OWNED BY "{role}"')
            c.exec_driver_sql(f'DROP ROLE "{role}"')


def test_restricted_login_role_can_login_and_use_real_api(runtime):
    _, _, engine, settings = runtime
    with engine.connect() as c:assert runtime_profile_matches(c)
    with TestClient(create_app(settings, engine), base_url='https://testserver',
                    headers={'Origin': 'https://testserver', 'X-Requested-With': 'marking-erp'}) as client:
        login(client, 'administrator')
        assert client.get('/api/users').status_code == 200
        assert client.get('/health/ready').status_code == 200
    with engine.begin() as c:
        c.execute(text("UPDATE groups SET revision=revision+1 WHERE slug='bm'"))
    report = inspect_runtime(settings, engine)
    assert report['ready'], report
    assert not report['business_integrations_verified']


@pytest.mark.parametrize('sql', ['CREATE TABLE forbidden(id integer)', 'ALTER TABLE users DISABLE TRIGGER ALL',
    "UPDATE alembic_version SET version_num=version_num", 'DELETE FROM audit_events WHERE false', 'TRUNCATE sessions'])
def test_runtime_cannot_change_schema_disable_guards_or_rewrite_audit(runtime, sql):
    with pytest.raises(DBAPIError) as caught:
        with runtime[2].begin() as c:c.exec_driver_sql(sql)
    assert caught.value.orig.sqlstate == '42501'


def test_missing_or_excessive_grants_fail_profile(runtime, engine):
    role, schema, runtime_engine, _ = runtime
    with engine.begin() as c:c.exec_driver_sql(f'GRANT UPDATE ON "{schema}".audit_events TO "{role}"')
    with runtime_engine.connect() as c:assert not runtime_profile_matches(c)
    with engine.begin() as c:
        c.exec_driver_sql(f'REVOKE UPDATE ON "{schema}".audit_events FROM "{role}"')
        c.exec_driver_sql(f'REVOKE USAGE ON ALL SEQUENCES IN SCHEMA "{schema}" FROM "{role}"')
    with runtime_engine.connect() as c:assert not runtime_profile_matches(c)


def test_owner_membership_and_superuser_not_accepted(runtime, engine):
    role, schema, runtime_engine, _ = runtime
    with engine.begin() as c:
        owner = c.execute(text('SELECT current_user')).scalar_one()
        c.exec_driver_sql(f'GRANT "{owner}" TO "{role}"')
    with runtime_engine.connect() as c:assert not runtime_profile_matches(c)
    with engine.begin() as c:
        with pytest.raises(ValueError):apply_runtime_grants(c, owner, schema)


def test_wrong_mfa_key_fails_without_logging_ciphertext_or_dsn(runtime):
    _, _, engine, settings = runtime
    report = inspect_runtime(settings.model_copy(update={'mfa_encryption_key': SecretStr(Fernet.generate_key().decode())}), engine)
    assert not report['ready'] and not report['checks']['mfa_decrypt']
    assert 'postgresql' not in str(report)


def test_runtime_cannot_run_grant_profile_as_an_administrative_command(runtime):
    role, schema, engine, _ = runtime
    with engine.begin() as c:
        with pytest.raises(ValueError, match='runtime_grants_require_owner'):
            apply_runtime_grants(c, role, schema)

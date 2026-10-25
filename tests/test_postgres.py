"""Run only against an explicitly designated disposable PostgreSQL database."""
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
import pytest
from alembic import command
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError, DBAPIError
from marking_erp.config import Settings
from marking_erp.migrate import migration_config
from marking_erp.seed import seed_defaults
from marking_erp.web import create_app

pytestmark = pytest.mark.postgres


@pytest.fixture(scope="module")
def engine():
    url = os.environ.get("ERP_TEST_DATABASE_URL")
    if not url:
        if os.environ.get("CI"):
            pytest.fail("CI requires ERP_TEST_DATABASE_URL; PostgreSQL checks must not silently skip")
        pytest.skip("Set ERP_TEST_DATABASE_URL to a disposable PostgreSQL database")
    settings = Settings(database_url=url)
    engine = create_engine(settings.database_url.get_secret_value(), hide_parameters=True)
    # Unique schema isolates these tests; existing data and public schema are untouched.
    schema = "erp_test_" + uuid4().hex
    with engine.begin() as c:
        c.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def set_schema(dbapi_connection, record):
        previous = dbapi_connection.autocommit
        dbapi_connection.autocommit = True
        with dbapi_connection.cursor() as cursor:
            cursor.execute(f'SET search_path TO "{schema}"')
        dbapi_connection.autocommit = previous

    # Recycle the first pool connection, which predates the search_path listener.
    engine.dispose()
    try:
        yield engine
    finally:
        engine.dispose()
        with engine.begin() as c:
            c.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        engine.dispose()


@pytest.fixture(autouse=True)
def fresh_schema(engine):
    with engine.begin() as c:
        config = migration_config(); config.attributes["connection"] = c
        command.upgrade(config, "head")
    yield
    with engine.begin() as c:
        config = migration_config(); config.attributes["connection"] = c
        command.downgrade(config, "base")


def seed(engine):
    with engine.begin() as c:
        return seed_defaults(c)


def test_clean_install_seed_and_readiness(engine):
    settings = Settings(database_url="postgresql+psycopg://unused@localhost/unused", build_sha="test-build")
    with TestClient(create_app(settings, engine)) as client:
        assert client.get("/health/live").json() == {"status": "ok"}
        ready = client.get("/health/ready")
        assert ready.status_code == 200 and ready.json()["build_sha"] == "test-build"
        assert client.get("/api/warehouse/documents").status_code == 404
    assert seed(engine)
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM groups")).scalar() == 6
        assert c.execute(text("SELECT count(*) FROM permissions")).scalar() == 81
        assert c.execute(text("SELECT count(*) FROM group_grants")).scalar() == 486
        assert c.execute(text("SELECT count(*) FROM users")).scalar() == 0
        assert c.execute(text("SELECT count(*) FROM audit_events")).scalar() == 1


def test_seed_rerun_preserves_revocation_and_deleted_grant(engine):
    seed(engine)
    with engine.begin() as c:
        c.execute(text("UPDATE group_grants SET scope='none' WHERE group_slug='bm' AND permission_id='codes.verify'"))
        c.execute(text("DELETE FROM group_grants WHERE group_slug='warehouse' AND permission_id='labels.reprint'"))
    assert seed(engine) is False
    with engine.connect() as c:
        assert c.execute(text("SELECT scope FROM group_grants WHERE group_slug='bm' AND permission_id='codes.verify'")).scalar() == "none"
        assert c.execute(text("SELECT count(*) FROM group_grants WHERE group_slug='warehouse' AND permission_id='labels.reprint'")).scalar() == 0


def test_concurrent_seed_initializes_once(engine):
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: seed(engine), range(2)))
    assert sorted(outcomes) == [False, True]
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM audit_events")).scalar() == 1


def test_specialist_cannot_gain_administrative_grant_or_reclassification(engine):
    seed(engine)
    with pytest.raises(DBAPIError), engine.begin() as c:
        c.execute(text("UPDATE group_grants SET scope='all' WHERE group_slug='specialist' AND permission_id='users.manage'"))
    with pytest.raises(DBAPIError), engine.begin() as c:
        c.execute(text("UPDATE permissions SET administrative=true WHERE id='codes.verify'"))


@pytest.mark.parametrize("sql", ["UPDATE audit_events SET action='changed'", "DELETE FROM audit_events", "TRUNCATE audit_events"])
def test_audit_is_append_only(engine, sql):
    seed(engine)
    with pytest.raises(DBAPIError), engine.begin() as c:
        c.execute(text(sql))


def test_invalid_scope_org_and_foreign_keys_fail(engine):
    seed(engine)
    with pytest.raises(IntegrityError), engine.begin() as c:
        c.execute(text("UPDATE group_grants SET scope='anything' WHERE group_slug='tsd'"))
    with pytest.raises(IntegrityError), engine.begin() as c:
        c.execute(text("INSERT INTO organizations(id,inn,name) VALUES (:id,'123','Invalid')"), {"id": uuid4()})
    with pytest.raises(IntegrityError), engine.begin() as c:
        c.execute(text("INSERT INTO user_organizations(user_id,organization_id) VALUES (:u,:o)"), {"u": uuid4(), "o": uuid4()})


def test_schema_drift_is_not_reported_ready(engine):
    with engine.begin() as c:
        c.execute(text("UPDATE alembic_version SET version_num='unexpected'"))
    settings = Settings(database_url="postgresql+psycopg://unused@localhost/unused")
    with TestClient(create_app(settings, engine)) as client:
        result = client.get("/health/ready")
        assert result.status_code == 503
        assert result.json()["reason"] == "schema_revision_mismatch"
    with engine.begin() as c:
        c.execute(text("UPDATE alembic_version SET version_num='0001_core'"))


def test_upgrade_downgrade_upgrade(engine):
    with engine.begin() as c:
        config = migration_config(); config.attributes["connection"] = c
        command.downgrade(config, "base")
        command.upgrade(config, "head")
    assert seed(engine)


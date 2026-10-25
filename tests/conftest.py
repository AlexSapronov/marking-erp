import os
from uuid import uuid4
import pytest
from alembic import command
from sqlalchemy import create_engine
from marking_erp.config import Settings
from marking_erp.migrate import migration_config

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


@pytest.fixture
def fresh_schema(engine):
    with engine.begin() as c:
        config = migration_config(); config.attributes["connection"] = c
        command.upgrade(config, "head")
    yield
    with engine.begin() as c:
        config = migration_config(); config.attributes["connection"] = c
        command.downgrade(config, "base")


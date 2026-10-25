from alembic import context
from sqlalchemy import create_engine, pool
from marking_erp.config import Settings

config = context.config


def configure(connection):
    context.configure(connection=connection, target_metadata=None)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    context.configure(url="postgresql+psycopg://", literal_binds=True, target_metadata=None)
    with context.begin_transaction():
        context.run_migrations()
elif config.attributes.get("connection") is not None:
    configure(config.attributes["connection"])
else:
    settings = Settings()
    engine = create_engine(settings.database_url.get_secret_value(), poolclass=pool.NullPool,
                           hide_parameters=True, connect_args={"connect_timeout": 5})
    try:
        with engine.connect() as connection:
            configure(connection)
    finally:
        engine.dispose()


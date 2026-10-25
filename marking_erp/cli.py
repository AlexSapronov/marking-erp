import argparse
from alembic import command
from sqlalchemy.exc import SQLAlchemyError
from .config import Settings
from .database import database_engine
from .migrate import migration_config
from .seed import seed_defaults


def main():
    parser = argparse.ArgumentParser(description="Marking ERP database infrastructure")
    parser.add_argument("action", choices=("migrate", "seed"))
    args = parser.parse_args()
    settings = Settings()
    engine = database_engine(settings)
    try:
        with engine.begin() as connection:
            if args.action == "migrate":
                # Serialize migrations across processes, not just in this CLI.
                from sqlalchemy import text
                connection.execute(text("SELECT pg_advisory_xact_lock(683273527)"))
                config = migration_config()
                config.attributes["connection"] = connection
                command.upgrade(config, "head")
            else:
                print("initialized" if seed_defaults(connection) else "already_initialized")
    except (SQLAlchemyError, ValueError):
        parser.exit(1, "Database operation failed; inspect schema/configuration without sharing credentials.\n")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()


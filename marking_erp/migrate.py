from importlib.resources import files
from alembic.config import Config
from alembic.script import ScriptDirectory


def migration_config():
    config = Config()
    config.set_main_option("script_location", str(files("marking_erp.migrations")))
    return config


def schema_head():
    return ScriptDirectory.from_config(migration_config()).get_current_head()


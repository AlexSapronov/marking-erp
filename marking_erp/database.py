from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from .config import Settings


def database_engine(settings: Settings) -> Engine:
    return create_engine(
        settings.database_url.get_secret_value(), pool_pre_ping=True,
        hide_parameters=True, pool_size=5, max_overflow=5,
        connect_args={"connect_timeout": 5, "options": "-c statement_timeout=5000"},
    )


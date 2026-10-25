from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ERP_", extra="ignore", hide_input_in_errors=True)
    database_url: SecretStr
    build_sha: str = "development"

    @field_validator("database_url")
    @classmethod
    def validate_database(cls, secret: SecretStr) -> SecretStr:
        try:
            url = make_url(secret.get_secret_value())
            if url.drivername != "postgresql+psycopg" or not url.database or not url.username or not url.host:
                raise ValueError
        except Exception:
            # Do not echo the DSN, including passwords, in validation errors.
            raise ValueError("ERP_DATABASE_URL must identify a PostgreSQL database using psycopg") from None
        return secret


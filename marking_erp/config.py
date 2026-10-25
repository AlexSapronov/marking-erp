from pathlib import Path
from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from urllib.parse import urlsplit
from pydantic import model_validator
from cryptography.fernet import Fernet


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ERP_", extra="ignore", hide_input_in_errors=True)
    database_url: SecretStr
    file_storage_path: Path = Path('/var/lib/marking-erp/files')
    build_sha: str = "development"
    public_url: str = "https://localhost"
    secure_cookie: bool = True
    mfa_encryption_key: SecretStr | None = None

    @field_validator("mfa_encryption_key")
    @classmethod
    def validate_mfa_key(cls, value):
        if value is not None:
            try:
                Fernet(value.get_secret_value().encode())
            except (ValueError, TypeError):
                raise ValueError("ERP_MFA_ENCRYPTION_KEY must be a valid Fernet key") from None
        return value

    @model_validator(mode="after")
    def validate_public_url(self):
        parsed = urlsplit(self.public_url)
        if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
            raise ValueError("ERP_PUBLIC_URL must be a base origin")
        if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1") and not self.secure_cookie):
            raise ValueError("HTTPS is required except for explicit local development")
        if not self.secure_cookie and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("Insecure cookies are allowed only on loopback")
        self.public_url = self.public_url.rstrip("/")
        return self

    @property
    def cookie_name(self):
        return "__Host-erp_session" if self.secure_cookie else "erp_session"

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


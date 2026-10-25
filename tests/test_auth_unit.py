import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from cryptography.fernet import Fernet
import pyotp
from marking_erp import auth
from marking_erp.config import Settings
from marking_erp.web import create_app


def test_password_hash_is_salted_and_does_not_accept_other_password():
    first = auth.password_hash("correct-personal-password")
    second = auth.password_hash("correct-personal-password")
    assert first != second
    assert auth.password_matches("correct-personal-password", first)
    assert not auth.password_matches("incorrect-personal-password", first)
    assert not auth.password_matches("anything", "broken")
    with pytest.raises(ValueError):
        auth.password_hash("short")


def test_mfa_encryption_replay_and_corruption(monkeypatch):
    settings = Settings(database_url="postgresql+psycopg://unused@localhost/test", mfa_encryption_key=Fernet.generate_key().decode())
    secret = pyotp.random_base32()
    cipher = auth.encrypt_mfa(secret, settings)
    assert secret.encode() not in cipher
    monkeypatch.setattr(auth.time, "time", lambda: 1800000000)
    code = pyotp.TOTP(secret).at(1800000000)
    counter = auth.mfa_counter(cipher, code, settings)
    assert counter == 60000000
    assert auth.mfa_counter(cipher, code, settings, last=counter) is None
    assert auth.mfa_counter(cipher[:-3]+b"bad", code, settings) is None


def test_local_http_is_explicit_and_remote_http_is_forbidden():
    dsn = "postgresql+psycopg://unused@localhost/test"
    with pytest.raises(ValidationError):
        Settings(database_url=dsn, public_url="http://corporate.example", secure_cookie=False)
    with pytest.raises(ValidationError):
        Settings(database_url=dsn, public_url="https://corporate.example", secure_cookie=False)
    local = Settings(database_url=dsn, public_url="http://127.0.0.1:8000", secure_cookie=False)
    assert local.cookie_name == "erp_session"


def test_login_origin_and_invalid_input_never_echo_credentials():
    settings = Settings(database_url="postgresql+psycopg://unused@localhost/test", public_url="https://testserver")
    with TestClient(create_app(settings), base_url="https://testserver") as client:
        assert client.post("/api/session", json={"username":"u", "password":"secret"}).status_code == 403
        headers = {"Origin":"https://testserver", "X-Requested-With":"marking-erp"}
        secret = "must-never-be-echoed"
        result = client.post("/api/session", headers=headers, json={"username":"u", "password":{"leaked":secret}})
        assert result.status_code == 422 and secret not in result.text


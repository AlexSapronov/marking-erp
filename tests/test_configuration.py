import json
from pathlib import Path
import pytest
from pydantic import ValidationError
from marking_erp.config import Settings
from marking_erp.migrate import schema_head
from marking_erp.seed import permission_defaults, GROUPS


def test_only_explicit_postgres_configuration_is_accepted(monkeypatch):
    monkeypatch.delenv("ERP_DATABASE_URL", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
    secret = "do-not-echo-this-password"
    with pytest.raises(ValidationError) as error:
        Settings(database_url=f"sqlite://user:{secret}@host/database")
    assert secret not in str(error.value)
    assert "postgresql" in str(error.value).casefold()
    settings = Settings(database_url=f"postgresql+psycopg://user:{secret}@localhost/database")
    assert secret not in repr(settings)


def test_packaged_rbac_matches_reviewed_matrix():
    reviewed = json.loads((Path(__file__).parents[1]/"docs/architecture/permission-defaults.json").read_text())
    actual = permission_defaults()
    assert actual == reviewed
    assert len(actual) == len({p["id"] for p in actual})
    assert set(GROUPS) == {"bm", "logistics", "warehouse", "tsd", "specialist", "administrator"}
    for permission in actual:
        assert set(permission["defaults"]) == set(GROUPS)
        assert permission["defaults"]["specialist"] == ("-" if permission["administrative"] else "all")
        assert permission["defaults"]["administrator"] == "all"


def test_packaged_migrations_have_one_head():
    assert schema_head() == "0021_workflow_policy"


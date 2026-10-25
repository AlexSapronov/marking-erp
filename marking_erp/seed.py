import json
from importlib.resources import files
from sqlalchemy import text

GROUPS = {
    "bm": "БМ", "logistics": "Логист", "warehouse": "Склад", "tsd": "ТСД",
    "specialist": "Специалист по маркировке", "administrator": "Администратор",
}
SEED_KEY = "rbac_defaults_v1"


def permission_defaults():
    return json.loads(files("marking_erp.data").joinpath("permission-defaults.json").read_text())


def seed_defaults(connection):
    """Caller owns the transaction. A rerun never restores revoked grants."""
    connection.execute(text("SELECT pg_advisory_xact_lock(683273526)"))
    if connection.execute(text("SELECT 1 FROM seed_state WHERE key=:key"), {"key": SEED_KEY}).scalar():
        return False
    # A non-empty manual RBAC configuration must be reconciled explicitly.
    if connection.execute(text("SELECT count(*) FROM groups")).scalar():
        raise ValueError("RBAC is already configured; default initialization refused")
    connection.execute(text("INSERT INTO groups(slug,name) VALUES (:slug,:name)"),
                       [{"slug": slug, "name": name} for slug, name in GROUPS.items()])
    permissions = permission_defaults()
    connection.execute(text("INSERT INTO permissions(id,administrative) VALUES (:id,:administrative)"),
                       [{"id": p["id"], "administrative": p["administrative"]} for p in permissions])
    connection.execute(text("INSERT INTO group_grants(group_slug,permission_id,scope) VALUES (:group,:permission,:scope)"),
                       [{"group": g, "permission": p["id"], "scope": "none" if scope == "-" else scope}
                        for p in permissions for g, scope in p["defaults"].items()])
    connection.execute(text("INSERT INTO seed_state(key) VALUES (:key)"), {"key": SEED_KEY})
    connection.execute(text("INSERT INTO audit_events(action,entity,detail) VALUES ('rbac.initialized',:key,CAST(:detail AS jsonb))"),
                       {"key": SEED_KEY, "detail": json.dumps({"groups": len(GROUPS), "permissions": len(permissions)})})
    return True


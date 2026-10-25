"""Organizations, identities, RBAC defaults and append-only audit."""
from importlib.resources import files
from alembic import op

revision = "0001_core"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.execute(files("marking_erp.migrations").joinpath("0001_core.sql").read_text())


def downgrade():
    for table in ("seed_state", "audit_events", "user_organizations", "users", "group_grants", "permissions", "groups", "organizations"):
        op.execute(f"DROP TABLE {table}")
    op.execute("DROP FUNCTION reject_audit_change()")
    op.execute("DROP FUNCTION validate_specialist_grant()")
    op.execute("DROP FUNCTION validate_permission_classification()")


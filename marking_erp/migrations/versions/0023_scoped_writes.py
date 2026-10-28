"""Scalable row locks for large sets of warehouse CIS and invoice GTIN."""
from alembic import op
revision='0023_scoped_writes'
down_revision='0022_worker_health'
branch_labels=None
depends_on=None


def upgrade():
    # Only opaque hashes, no extra copies of codes or business document fields.
    # Row locks do not consume one shared-memory advisory slot per resource.
    op.execute('''CREATE TABLE write_resources(key char(64) PRIMARY KEY
        CHECK(key ~ '^[0-9a-f]{64}$'))''')
    op.execute('CREATE TRIGGER write_resources_no_delete BEFORE DELETE OR TRUNCATE ON write_resources FOR EACH STATEMENT EXECUTE FUNCTION reject_audit_change()')


def downgrade():
    op.execute('DROP TABLE write_resources')

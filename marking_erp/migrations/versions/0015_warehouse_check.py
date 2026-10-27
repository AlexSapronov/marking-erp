"""Expected payload evidence and revision-bound batch warehouse checks."""
from alembic import op
revision='0015_warehouse_check'
down_revision='0014_incidents'
branch_labels=None
depends_on=None


def upgrade():
    op.execute("ALTER TABLE warehouse_tasks ADD COLUMN expected_payloads jsonb NOT NULL DEFAULT '{}' CHECK(jsonb_typeof(expected_payloads)='object'), ADD COLUMN transfer_request_sha256 char(64), ADD COLUMN check_result jsonb, ADD COLUMN check_digest char(64), ADD COLUMN check_policy_sha256 char(64)")
    op.execute("CREATE UNIQUE INDEX warehouse_check_request ON warehouse_events((detail->>'request_id')) WHERE action='checked' AND detail ? 'request_id'")


def downgrade():
    op.execute('DROP INDEX warehouse_check_request')
    op.execute('ALTER TABLE warehouse_tasks DROP COLUMN check_policy_sha256,DROP COLUMN check_digest,DROP COLUMN check_result,DROP COLUMN transfer_request_sha256,DROP COLUMN expected_payloads')

"""Indexed lookup of expected and scanned codes in active warehouse tasks.

Existing overlaps are kept for explicit operator resolution; no history or
expectations are silently rewritten by the migration.
"""
from alembic import op
revision='0016_warehouse_overlap'
down_revision='0015_warehouse_check'
branch_labels=None
depends_on=None


def upgrade():
    op.execute("CREATE INDEX warehouse_open_expected ON warehouse_tasks USING gin(expected_codes) WHERE state='open'")
    op.execute('CREATE INDEX warehouse_active_cis ON warehouse_scans(cis,task_id) WHERE removed_at IS NULL')


def downgrade():
    op.execute('DROP INDEX warehouse_active_cis')
    op.execute('DROP INDEX warehouse_open_expected')

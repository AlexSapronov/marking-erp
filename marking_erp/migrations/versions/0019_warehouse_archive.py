"""Archive visibility without rewriting warehouse completion evidence."""
from alembic import op
revision='0019_warehouse_archive'
down_revision='0018_bm_bulk'
branch_labels=None
depends_on=None


def upgrade():
    op.execute('ALTER TABLE warehouse_tasks ADD COLUMN archived_at timestamptz, ADD COLUMN archived_by uuid REFERENCES users(id)')
    op.execute("ALTER TABLE warehouse_tasks ADD CONSTRAINT warehouse_archive_closed CHECK(archived_at IS NULL OR state='closed')")


def downgrade():
    op.execute('ALTER TABLE warehouse_tasks DROP CONSTRAINT warehouse_archive_closed, DROP COLUMN archived_by, DROP COLUMN archived_at')

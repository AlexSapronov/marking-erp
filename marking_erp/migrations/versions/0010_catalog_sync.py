"""Deduplicate NK sync windows without storing unbounded ID arrays in a job."""
from alembic import op
revision='0010_catalog_sync'
down_revision='0009_code_registry'
branch_labels=None
depends_on=None


def upgrade():
    op.execute('''CREATE TABLE catalog_sync_entries(
        item_id uuid NOT NULL REFERENCES business_items(id),good_id text NOT NULL CHECK(length(good_id) BETWEEN 1 AND 100),
        created_at timestamptz NOT NULL DEFAULT now(),PRIMARY KEY(item_id,good_id))''')
    op.execute('CREATE TRIGGER catalog_sync_entries_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON catalog_sync_entries FOR EACH STATEMENT EXECUTE FUNCTION reject_audit_change()')


def downgrade():op.execute('DROP TABLE catalog_sync_entries')

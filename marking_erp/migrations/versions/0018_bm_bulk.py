"""Immutable BM bulk request receipts."""
from alembic import op
revision='0018_bm_bulk'
down_revision='0017_warehouse_import'
branch_labels=None
depends_on=None


def upgrade():
    op.execute('''CREATE TABLE bm_bulk_receipts(id uuid PRIMARY KEY,organization_id uuid NOT NULL REFERENCES organizations(id),
        actor_id uuid NOT NULL REFERENCES users(id),fingerprint char(64) NOT NULL,results jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now())''')
    op.execute('CREATE TRIGGER bm_bulk_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON bm_bulk_receipts FOR EACH STATEMENT EXECUTE FUNCTION reject_audit_change()')


def downgrade():op.execute('DROP TABLE bm_bulk_receipts')

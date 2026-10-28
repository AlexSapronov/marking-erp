"""Durable one-shot automatic printing intents and exact part membership."""
from alembic import op
revision='0020_auto_print'
down_revision='0019_warehouse_archive'
branch_labels=None
depends_on=None


def upgrade():
    op.execute('''CREATE TABLE auto_print_requests(id uuid PRIMARY KEY,
        item_id uuid NOT NULL REFERENCES business_items(id),organization_id uuid NOT NULL REFERENCES organizations(id),
        actor_id uuid NOT NULL REFERENCES users(id),device_id uuid NOT NULL REFERENCES printer_devices(id),
        bundle_id uuid REFERENCES business_artifacts(id),request_sha256 char(64) NOT NULL,
        state text NOT NULL CHECK(state IN ('waiting','queued','blocked','cancelled')),
        reason text NOT NULL DEFAULT '',created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now())''')
    op.execute("CREATE UNIQUE INDEX auto_print_active ON auto_print_requests(item_id) WHERE state IN ('waiting','blocked')")
    op.execute("CREATE INDEX auto_print_waiting ON auto_print_requests(updated_at,id) WHERE state='waiting'")
    op.execute('ALTER TABLE print_jobs ADD COLUMN auto_request_id uuid REFERENCES auto_print_requests(id), ADD COLUMN part_index integer CHECK(part_index>0)')
    op.execute('CREATE UNIQUE INDEX auto_print_part ON print_jobs(auto_request_id,artifact_id) WHERE auto_request_id IS NOT NULL')


def downgrade():
    op.execute('DROP INDEX auto_print_part')
    op.execute('ALTER TABLE print_jobs DROP COLUMN auto_request_id,DROP COLUMN part_index')
    op.execute('DROP TABLE auto_print_requests')

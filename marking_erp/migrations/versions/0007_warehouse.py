"""Warehouse assignments and physical scans, independent of external marking status."""
from alembic import op
revision = '0007_warehouse'
down_revision = '0006_bm_documents'
branch_labels = None
depends_on = None


def upgrade():
    op.execute('''CREATE TABLE warehouse_tasks(
        id uuid PRIMARY KEY, organization_id uuid NOT NULL REFERENCES organizations(id),
        owner_id uuid NOT NULL REFERENCES users(id), source_id uuid NOT NULL REFERENCES bm_documents(id),
        source_revision bigint NOT NULL CHECK(source_revision>=1), kind text NOT NULL CHECK(kind IN ('receipt','shipment')),
        number text NOT NULL, supplier text NOT NULL, document_date date NOT NULL,
        lines jsonb NOT NULL CHECK(jsonb_typeof(lines)='array' AND jsonb_array_length(lines) BETWEEN 1 AND 20000), state text NOT NULL DEFAULT 'open' CHECK(state IN ('open','closed','cancelled')),
        revision bigint NOT NULL DEFAULT 1 CHECK(revision>=1), created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(), closed_at timestamptz,
        FOREIGN KEY(id,organization_id) REFERENCES operation_targets(id,organization_id),
        FOREIGN KEY(source_id,organization_id) REFERENCES bm_documents(id,organization_id))''')
    op.execute("CREATE UNIQUE INDEX warehouse_source_active ON warehouse_tasks(source_id,kind) WHERE state<>'cancelled'")
    op.execute('CREATE INDEX warehouse_tasks_list ON warehouse_tasks(organization_id,kind,id)')
    op.execute('''CREATE TABLE warehouse_scans(
        id uuid PRIMARY KEY, task_id uuid NOT NULL REFERENCES warehouse_tasks(id),
        line_index integer NOT NULL CHECK(line_index>=0), cis text NOT NULL, gtin char(14) NOT NULL CHECK(valid_gtin14(gtin)),
        quantity bigint NOT NULL CHECK(quantity BETWEEN 1 AND 99999999), payload bytea NOT NULL CHECK(octet_length(payload) BETWEEN 1 AND 1024),
        actor_id uuid NOT NULL REFERENCES users(id), created_at timestamptz NOT NULL DEFAULT now(),
        removed_at timestamptz, removed_by uuid REFERENCES users(id))''')
    op.execute('CREATE UNIQUE INDEX warehouse_scan_active ON warehouse_scans(task_id,cis) WHERE removed_at IS NULL')
    op.execute('''CREATE TABLE warehouse_events(
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, task_id uuid NOT NULL REFERENCES warehouse_tasks(id),
        actor_id uuid NOT NULL REFERENCES users(id), action text NOT NULL, detail jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now())''')
    op.execute('CREATE TRIGGER warehouse_events_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON warehouse_events FOR EACH STATEMENT EXECUTE FUNCTION reject_audit_change()')


def downgrade():
    op.execute('DROP TABLE warehouse_events,warehouse_scans,warehouse_tasks')

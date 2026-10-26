"""Persistent cards, templates, marking runs, ATK and split workspaces."""
from alembic import op
revision='0008_business'
down_revision='0007_warehouse'
branch_labels=None
depends_on=None

def upgrade():
    op.execute("ALTER TABLE warehouse_tasks ADD COLUMN expected_codes jsonb NOT NULL DEFAULT '[]', ADD COLUMN verified_at timestamptz")
    op.execute('ALTER TABLE warehouse_scans ADD COLUMN verification jsonb')
    op.execute('CREATE INDEX bm_documents_chronological ON bm_documents(organization_id,created_at DESC,id DESC)')
    op.execute('CREATE INDEX warehouse_tasks_chronological ON warehouse_tasks(organization_id,kind,created_at DESC,id DESC)')
    op.execute('''CREATE TABLE business_items(
        id uuid PRIMARY KEY,organization_id uuid NOT NULL REFERENCES organizations(id),owner_id uuid NOT NULL REFERENCES users(id),
        kind text NOT NULL CHECK(kind IN ('card','template','import','marking','atk','transform','split','sync','fts')),
        title text NOT NULL CHECK(length(title) BETWEEN 1 AND 300),state text NOT NULL DEFAULT 'draft',
        revision bigint NOT NULL DEFAULT 1 CHECK(revision>0),body jsonb NOT NULL CHECK(jsonb_typeof(body)='object'),
        source_id uuid,created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now(),
        FOREIGN KEY(id,organization_id) REFERENCES operation_targets(id,organization_id))''')
    op.execute('CREATE TABLE signer_verifiers(device_id uuid PRIMARY KEY REFERENCES signer_devices(id),enabled boolean NOT NULL DEFAULT true)')
    op.execute('CREATE INDEX business_item_list ON business_items(organization_id,kind,created_at DESC,id DESC)')
    op.execute("CREATE UNIQUE INDEX business_active_marking ON business_items(source_id) WHERE kind='marking' AND state<>'archived'")
    op.execute('''CREATE TABLE business_events(id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,item_id uuid NOT NULL REFERENCES business_items(id),
        actor_id uuid NOT NULL REFERENCES users(id),action text NOT NULL,body jsonb NOT NULL,created_at timestamptz NOT NULL DEFAULT now())''')
    op.execute('CREATE TRIGGER business_events_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON business_events FOR EACH STATEMENT EXECUTE FUNCTION reject_audit_change()')
    op.execute('''CREATE TABLE business_links(operation_id uuid PRIMARY KEY REFERENCES external_operations(id),item_id uuid NOT NULL REFERENCES business_items(id),
        step text NOT NULL,metadata jsonb NOT NULL,applied boolean NOT NULL DEFAULT false,created_at timestamptz NOT NULL DEFAULT now())''')
    op.execute('''CREATE TABLE business_artifacts(id uuid PRIMARY KEY,item_id uuid NOT NULL REFERENCES business_items(id),kind text NOT NULL,
        version bigint NOT NULL,filename text NOT NULL,media_type text NOT NULL,body bytea NOT NULL CHECK(octet_length(body) BETWEEN 1 AND 16777216),
        sha256 char(64) NOT NULL,snapshot jsonb NOT NULL,created_at timestamptz NOT NULL DEFAULT now(),UNIQUE(item_id,kind,version))''')
    op.execute('CREATE TRIGGER business_artifacts_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON business_artifacts FOR EACH STATEMENT EXECUTE FUNCTION reject_audit_change()')
    op.execute('''CREATE TABLE business_codes(id uuid PRIMARY KEY,organization_id uuid NOT NULL REFERENCES organizations(id),cis text NOT NULL,
        gtin text NOT NULL CHECK(valid_gtin14(gtin)),base bytea NOT NULL,payload bytea NOT NULL,
        item_id uuid REFERENCES business_items(id),line_index integer,quantity bigint NOT NULL DEFAULT 1,
        state text NOT NULL CHECK(state IN ('reserved','emitted','applied','free','withdrawn')),tnved text NOT NULL,pn text NOT NULL,
        evidence_ref text NOT NULL,created_at timestamptz NOT NULL DEFAULT now(),UNIQUE(organization_id,cis))''')
    op.execute('''CREATE TABLE integration_settings(organization_id uuid PRIMARY KEY REFERENCES organizations(id),
        revision bigint NOT NULL DEFAULT 1,public jsonb NOT NULL,secret bytea NOT NULL,updated_at timestamptz NOT NULL DEFAULT now())''')

def downgrade():
    op.execute('DROP INDEX warehouse_tasks_chronological,bm_documents_chronological')
    op.execute('ALTER TABLE warehouse_scans DROP COLUMN verification')
    op.execute('ALTER TABLE warehouse_tasks DROP COLUMN expected_codes,DROP COLUMN verified_at')
    for t in ('signer_verifiers','integration_settings','business_codes','business_artifacts','business_links','business_events','business_items'):op.execute('DROP TABLE '+t)

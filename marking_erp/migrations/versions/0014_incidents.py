"""Incident registry, immutable history and explicit local quarantine."""
from alembic import op
revision='0014_incidents'
down_revision='0013_official_flows'
branch_labels=None
depends_on=None

def upgrade():
    op.execute('''CREATE TABLE incidents(id uuid PRIMARY KEY,organization_id uuid NOT NULL REFERENCES organizations(id),
        reporter_id uuid NOT NULL REFERENCES users(id),assignee_id uuid REFERENCES users(id),
        source_kind text NOT NULL CHECK(source_kind IN ('none','task','document','business')),
        source_id uuid REFERENCES operation_targets(id),source_title text NOT NULL,
        operation_id uuid REFERENCES external_operations(id),cis text,
        category text NOT NULL CHECK(category IN ('foreign_owner','invalid_status','damaged_label','duplicate_code','composition_mismatch','shortage','remote_error','unknown_operation','other')),
        description text NOT NULL CHECK(length(btrim(description)) BETWEEN 8 AND 5000),
        status text NOT NULL DEFAULT 'open' CHECK(status IN ('open','in_progress','resolved')),
        restrictions text[] NOT NULL DEFAULT '{}',revision bigint NOT NULL DEFAULT 1 CHECK(revision>0),request_sha256 char(64) NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now(),resolved_at timestamptz,
        CHECK(restrictions<@ARRAY['receipt','shipment','split','reprint','external','remark']::text[]),
        CHECK((source_kind='none')=(source_id IS NULL)),CHECK(status<>'resolved' OR cardinality(restrictions)=0),
        CHECK(cis IS NULL OR (length(cis)=38 AND substr(cis,1,2)='01' AND valid_gtin14(substr(cis,3,14)))))''')
    op.execute('CREATE INDEX incident_list ON incidents(organization_id,created_at DESC,id DESC)')
    op.execute("CREATE INDEX incident_quarantine ON incidents(organization_id,cis) WHERE status<>'resolved' AND cardinality(restrictions)>0")
    op.execute('''CREATE TABLE incident_events(id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        incident_id uuid NOT NULL REFERENCES incidents(id),actor_id uuid NOT NULL REFERENCES users(id),request_id uuid NOT NULL,
        request_sha256 char(64) NOT NULL,action text NOT NULL,body jsonb NOT NULL,result jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),UNIQUE(incident_id,request_id))''')
    op.execute('CREATE TRIGGER incident_events_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON incident_events FOR EACH STATEMENT EXECUTE FUNCTION reject_audit_change()')

def downgrade():op.execute('DROP TABLE incident_events,incidents')

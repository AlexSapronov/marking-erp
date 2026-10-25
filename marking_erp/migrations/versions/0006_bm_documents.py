"""Versioned local invoice preparation, isolated from external dispatch."""
from alembic import op
revision='0006_bm_documents'
down_revision='0005_operations'
branch_labels=None
depends_on=None


def upgrade():
    op.execute('''CREATE TABLE bm_documents(
        id uuid PRIMARY KEY,organization_id uuid NOT NULL REFERENCES organizations(id),
        owner_id uuid NOT NULL REFERENCES users(id),number text NOT NULL CHECK(length(btrim(number)) BETWEEN 1 AND 120),
        document_date date NOT NULL,supplier text NOT NULL CHECK(length(btrim(supplier)) BETWEEN 1 AND 200),
        revision bigint NOT NULL CHECK(revision>=1),state text NOT NULL CHECK(state IN ('draft','prepared','archived')),
        lines jsonb NOT NULL CHECK(jsonb_typeof(lines)='array' AND jsonb_array_length(lines) BETWEEN 1 AND 20000),
        policy_snapshot jsonb NOT NULL,request_sha256 text NOT NULL CHECK(request_sha256 ~ '^[a-f0-9]{64}$'),
        created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE(id,organization_id),UNIQUE(organization_id,owner_id,supplier,number,document_date),
        FOREIGN KEY(id,organization_id) REFERENCES operation_targets(id,organization_id))''')
    op.execute('CREATE INDEX bm_document_list ON bm_documents(organization_id,state,id)')
    op.execute('''CREATE TABLE bm_document_versions(
        document_id uuid NOT NULL,organization_id uuid NOT NULL,revision bigint NOT NULL CHECK(revision>=1),
        actor_id uuid NOT NULL REFERENCES users(id),action text NOT NULL CHECK(action IN ('created','edited','prepared','archived','restored')),
        reason text NOT NULL,snapshot jsonb NOT NULL,created_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY(document_id,revision),FOREIGN KEY(document_id,organization_id) REFERENCES bm_documents(id,organization_id))''')
    op.execute('CREATE TRIGGER bm_versions_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON bm_document_versions FOR EACH STATEMENT EXECUTE FUNCTION reject_audit_change()')


def downgrade():
    op.execute('DROP TABLE bm_document_versions')
    op.execute('DROP TABLE bm_documents')

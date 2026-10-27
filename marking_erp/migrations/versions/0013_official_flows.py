"""Official remarking workspace and delegated certificate token renewal."""
from alembic import op
revision='0013_official_flows'
down_revision='0012_label_jobs'
branch_labels=None
depends_on=None

def upgrade():
    op.execute('ALTER TABLE business_items DROP CONSTRAINT business_items_kind_check')
    op.execute("ALTER TABLE business_items ADD CONSTRAINT business_items_kind_check CHECK(kind IN ('card','template','import','marking','atk','transform','split','sync','fts','remark'))")
    op.execute('''CREATE TABLE integration_auth_jobs(id uuid PRIMARY KEY,organization_id uuid NOT NULL REFERENCES organizations(id),
        actor_id uuid NOT NULL REFERENCES users(id),service text NOT NULL CHECK(service IN ('true','oms')),
        challenge_uuid uuid NOT NULL,challenge bytea NOT NULL,payload_sha256 char(64) NOT NULL,
        connection_revision bigint NOT NULL,oms_connection text NOT NULL DEFAULT '',
        state text NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','leased','confirmed','failed','expired')),
        device_id uuid REFERENCES signer_devices(id),lease_hash char(64),expires_at timestamptz NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),finished_at timestamptz,error text NOT NULL DEFAULT '')''')
    op.execute("CREATE UNIQUE INDEX integration_auth_pending ON integration_auth_jobs(organization_id,service) WHERE state IN ('pending','leased')")
    op.execute('''CREATE TABLE integration_auth_state(organization_id uuid NOT NULL REFERENCES organizations(id),service text NOT NULL,
        renewed_at timestamptz,next_refresh_at timestamptz NOT NULL DEFAULT now(),error text NOT NULL DEFAULT '',PRIMARY KEY(organization_id,service))''')
    op.execute("CREATE UNIQUE INDEX business_active_remark ON business_items(organization_id,(body->>'old_cis')) WHERE kind='remark' AND state NOT IN ('archived','done')")

    op.execute("""CREATE TABLE code_replacements(old_code_id uuid PRIMARY KEY REFERENCES business_codes(id),
        new_code_id uuid NOT NULL UNIQUE REFERENCES business_codes(id),remark_id uuid NOT NULL UNIQUE REFERENCES business_items(id),
        external_document_id uuid NOT NULL,created_at timestamptz NOT NULL DEFAULT now(),CHECK(old_code_id<>new_code_id))""")
    op.execute('CREATE TRIGGER code_replacements_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON code_replacements FOR EACH STATEMENT EXECUTE FUNCTION reject_audit_change()')

def downgrade():
    op.execute('DROP TABLE code_replacements')
    op.execute('DROP INDEX business_active_remark');op.execute('DROP TABLE integration_auth_state,integration_auth_jobs')
    op.execute('ALTER TABLE business_items DROP CONSTRAINT business_items_kind_check')
    op.execute("ALTER TABLE business_items ADD CONSTRAINT business_items_kind_check CHECK(kind IN ('card','template','import','marking','atk','transform','split','sync','fts')) NOT VALID")

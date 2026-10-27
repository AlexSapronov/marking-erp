"""Durable printing and policy-bound reprint requests."""
from alembic import op
revision='0012_label_jobs'
down_revision='0011_suppliers'
branch_labels=None
depends_on=None

def upgrade():
    op.execute('''CREATE TABLE printer_devices(id uuid PRIMARY KEY,organization_id uuid NOT NULL REFERENCES organizations(id),
        name text NOT NULL,printer_name text NOT NULL,token_hash char(64) NOT NULL UNIQUE,enabled boolean NOT NULL DEFAULT true,
        last_seen_at timestamptz,created_at timestamptz NOT NULL DEFAULT now())''')
    op.execute('''CREATE TABLE reprint_requests(id uuid PRIMARY KEY,organization_id uuid NOT NULL REFERENCES organizations(id),
        actor_id uuid NOT NULL REFERENCES users(id),code_id uuid NOT NULL REFERENCES business_codes(id),
        artifact_id uuid NOT NULL REFERENCES business_artifacts(id),reason text NOT NULL,copies integer NOT NULL CHECK(copies BETWEEN 1 AND 10),
        payload_sha256 char(64) NOT NULL,policy jsonb NOT NULL,request_sha256 char(64) NOT NULL,
        state text NOT NULL DEFAULT 'prepared' CHECK(state IN ('prepared','printed','completed','cancelled')),
        scanned_copies integer NOT NULL DEFAULT 0,created_at timestamptz NOT NULL DEFAULT now(),completed_at timestamptz)''')
    op.execute('''CREATE TABLE print_jobs(id uuid PRIMARY KEY,organization_id uuid NOT NULL REFERENCES organizations(id),
        actor_id uuid NOT NULL REFERENCES users(id),device_id uuid NOT NULL REFERENCES printer_devices(id),
        artifact_id uuid NOT NULL REFERENCES business_artifacts(id),reprint_id uuid UNIQUE REFERENCES reprint_requests(id),
        copies integer NOT NULL CHECK(copies BETWEEN 1 AND 10),request_sha256 char(64) NOT NULL,
        state text NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','claimed','spooled','unknown','cancelled')),
        lease_hash char(64),claimed_at timestamptz,finished_at timestamptz,receipt jsonb,
        created_at timestamptz NOT NULL DEFAULT now())''')
    op.execute("CREATE INDEX print_job_queue ON print_jobs(device_id,created_at,id) WHERE state='queued'")
    op.execute('''CREATE TABLE print_events(id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,job_id uuid,
        actor_id uuid REFERENCES users(id),device_id uuid REFERENCES printer_devices(id),action text NOT NULL,body jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now())''')
    op.execute('CREATE TRIGGER print_events_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON print_events FOR EACH STATEMENT EXECUTE FUNCTION reject_audit_change()')
    op.execute('''CREATE TABLE reprint_scans(request_id uuid NOT NULL REFERENCES reprint_requests(id),request_key uuid NOT NULL,
        ordinal integer NOT NULL,payload_sha256 char(64) NOT NULL,actor_id uuid NOT NULL REFERENCES users(id),
        created_at timestamptz NOT NULL DEFAULT now(),PRIMARY KEY(request_id,request_key),UNIQUE(request_id,ordinal))''')
    op.execute('CREATE TRIGGER reprint_scans_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON reprint_scans FOR EACH STATEMENT EXECUTE FUNCTION reject_audit_change()')

def downgrade():
    for table in ('reprint_scans','print_events','print_jobs','reprint_requests','printer_devices'):op.execute('DROP TABLE '+table)

"""Durable commands, evidence, fencing and a separately authenticated signer."""
from alembic import op
revision='0005_operations'
down_revision='0004_registry'
branch_labels=None
depends_on=None


def upgrade():
    op.execute('''CREATE TABLE operation_contracts(kind text PRIMARY KEY,permission_id text NOT NULL REFERENCES permissions(id),
        version text NOT NULL CHECK(length(version)>0),semantic text NOT NULL CHECK(semantic IN ('read','mutation')),
        requires_signature boolean NOT NULL DEFAULT false,approved boolean NOT NULL DEFAULT false,evidence_ref text NOT NULL,
        CHECK(NOT requires_signature OR semantic='mutation'))''')
    op.execute('''CREATE TABLE operation_targets(id uuid PRIMARY KEY,organization_id uuid NOT NULL REFERENCES organizations(id),
        owner_id uuid NOT NULL REFERENCES users(id),revision bigint NOT NULL CHECK(revision>0),stopped boolean NOT NULL DEFAULT false,UNIQUE(id,organization_id))''')
    op.execute('''CREATE TABLE operation_assignments(target_id uuid NOT NULL REFERENCES operation_targets(id),user_id uuid NOT NULL REFERENCES users(id),PRIMARY KEY(target_id,user_id))''')
    op.execute('''CREATE TABLE external_operations(id uuid PRIMARY KEY,organization_id uuid NOT NULL REFERENCES organizations(id),actor_id uuid NOT NULL REFERENCES users(id),
        kind text NOT NULL REFERENCES operation_contracts(kind),contract_version text NOT NULL,semantic text NOT NULL CHECK(semantic IN ('read','mutation')),
        permission_id text NOT NULL REFERENCES permissions(id),requires_signature boolean NOT NULL,target_id uuid NOT NULL,target_revision bigint NOT NULL,
        idempotency_key text NOT NULL CHECK(length(idempotency_key) BETWEEN 1 AND 120),intent_sha256 char(64) NOT NULL,
        payload bytea NOT NULL CHECK(octet_length(payload) BETWEEN 1 AND 1048576),payload_sha256 char(64) NOT NULL,policy_snapshot jsonb NOT NULL,
        state text NOT NULL CHECK(state IN ('awaiting_signature','queued','dispatching','submitted','confirmed','rejected','blocked','unknown','cancelled_before_dispatch')),
        reason text NOT NULL DEFAULT '',external_id text,created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now(),
        available_at timestamptz NOT NULL DEFAULT now(),fence bigint NOT NULL DEFAULT 0,lease_hash char(64),lease_until timestamptz,worker_id text,
        UNIQUE(organization_id,kind,idempotency_key),UNIQUE(id,organization_id),FOREIGN KEY(target_id,organization_id) REFERENCES operation_targets(id,organization_id))''')
    op.execute('''CREATE FUNCTION preserve_operation_command() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF (to_jsonb(NEW)-ARRAY['state','reason','external_id','updated_at','available_at','fence','lease_hash','lease_until','worker_id'])
         IS DISTINCT FROM (to_jsonb(OLD)-ARRAY['state','reason','external_id','updated_at','available_at','fence','lease_hash','lease_until','worker_id']) THEN
        RAISE EXCEPTION 'prepared command is immutable';
      END IF;
      RETURN NEW;
    END $$''')
    op.execute('CREATE TRIGGER operation_command BEFORE UPDATE ON external_operations FOR EACH ROW EXECUTE FUNCTION preserve_operation_command()')
    op.execute('CREATE TRIGGER operation_no_delete BEFORE DELETE OR TRUNCATE ON external_operations FOR EACH STATEMENT EXECUTE FUNCTION reject_audit_change()')
    op.execute('''CREATE TABLE operation_attempts(id uuid PRIMARY KEY,operation_id uuid NOT NULL REFERENCES external_operations(id),fence bigint NOT NULL,
        worker_id text NOT NULL,created_at timestamptz NOT NULL DEFAULT now(),UNIQUE(operation_id,fence))''')
    op.execute('''CREATE TABLE operation_receipts(id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,operation_id uuid NOT NULL REFERENCES external_operations(id),
        attempt_id uuid REFERENCES operation_attempts(id),fence bigint NOT NULL,phase text NOT NULL CHECK(phase IN ('dispatch','reconcile')),
        raw bytea NOT NULL CHECK(octet_length(raw) BETWEEN 1 AND 1048576),sha256 char(64) NOT NULL,applied boolean NOT NULL,classification text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now())''')
    op.execute('''CREATE TABLE operation_events(id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,operation_id uuid NOT NULL REFERENCES external_operations(id),
        state text NOT NULL,reason text NOT NULL,created_at timestamptz NOT NULL DEFAULT now())''')
    op.execute('''CREATE TABLE signer_devices(id uuid PRIMARY KEY,organization_id uuid NOT NULL REFERENCES organizations(id),name text NOT NULL,
        token_hash char(64) NOT NULL UNIQUE,certificate_sha256 text NOT NULL CHECK(certificate_sha256 ~ '^[a-f0-9]{64}$'),
        allowed_kinds text[] NOT NULL,enabled boolean NOT NULL DEFAULT true,last_seen_at timestamptz)''')
    op.execute('''CREATE TABLE signer_jobs(id uuid PRIMARY KEY,operation_id uuid NOT NULL UNIQUE REFERENCES external_operations(id),
        state text NOT NULL CHECK(state IN ('pending','leased','received','verified','rejected','cancelled')),
        device_id uuid REFERENCES signer_devices(id),certificate_sha256 char(64),fence bigint NOT NULL DEFAULT 0,
        lease_hash char(64),lease_until timestamptz,signature bytea,signature_sha256 char(64),verification_ref text,
        created_at timestamptz NOT NULL DEFAULT now(),CHECK(state<>'verified' OR (signature IS NOT NULL AND verification_ref IS NOT NULL)))''')
    op.execute('''CREATE TABLE signer_leases(job_id uuid NOT NULL REFERENCES signer_jobs(id),fence bigint NOT NULL,device_id uuid NOT NULL REFERENCES signer_devices(id),
        lease_hash char(64) NOT NULL,certificate_sha256 char(64) NOT NULL,payload_sha256 char(64) NOT NULL,created_at timestamptz NOT NULL DEFAULT now(),PRIMARY KEY(job_id,fence))''')
    op.execute('''CREATE TABLE signer_receipts(id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,job_id uuid NOT NULL REFERENCES signer_jobs(id),
        device_id uuid NOT NULL REFERENCES signer_devices(id),fence bigint NOT NULL,signature bytea NOT NULL CHECK(octet_length(signature) BETWEEN 1 AND 1048576),
        sha256 char(64) NOT NULL,applied boolean NOT NULL,created_at timestamptz NOT NULL DEFAULT now())''')
    for table in ('operation_attempts','operation_receipts','operation_events','signer_receipts','signer_leases'):
        op.execute(f'CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON {table} FOR EACH STATEMENT EXECUTE FUNCTION reject_audit_change()')
    op.execute("CREATE INDEX dispatch_ready ON external_operations(available_at,created_at) WHERE state='queued'")
    op.execute("CREATE INDEX dispatch_expiry ON external_operations(lease_until) WHERE state='dispatching'")


def downgrade():
    for table in ('signer_receipts','signer_leases','signer_jobs','signer_devices','operation_events','operation_receipts','operation_attempts','external_operations','operation_assignments','operation_targets','operation_contracts'):op.execute(f'DROP TABLE {table}')
    op.execute('DROP FUNCTION preserve_operation_command()')

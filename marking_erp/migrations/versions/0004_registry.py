"""Common identities, immutable payloads, evidence and file metadata."""
from alembic import op
revision='0004_registry'
down_revision='0003_policy'
branch_labels=None
depends_on=None


def upgrade():
    op.execute("""CREATE FUNCTION valid_gtin14(v text) RETURNS boolean LANGUAGE SQL IMMUTABLE STRICT AS $$
        SELECT CASE WHEN v ~ '^[0-9]{14}$' THEN
        (SELECT ((10-sum(substr(v,i,1)::int * CASE WHEN i%2=1 THEN 3 ELSE 1 END)::int%10)%10)=substr(v,14,1)::int FROM generate_series(1,13) i)
        ELSE false END $$""")
    op.execute('''CREATE TABLE products(id uuid PRIMARY KEY,gtin text NOT NULL UNIQUE CHECK(valid_gtin14(gtin)),created_at timestamptz NOT NULL DEFAULT now(),UNIQUE(id,gtin))''')
    op.execute('''CREATE TABLE product_pns(id uuid PRIMARY KEY,product_id uuid NOT NULL REFERENCES products(id),organization_id uuid NOT NULL REFERENCES organizations(id),
        pn text NOT NULL CHECK(length(btrim(pn)) BETWEEN 1 AND 120),tnved text NOT NULL CHECK(tnved ~ '^[0-9]{10}$'),
        source text NOT NULL CHECK(source IN ('nk_confirmed','external_verified')),source_ref text NOT NULL CHECK(length(btrim(source_ref))>0),
        evidence_sha256 text NOT NULL CHECK(evidence_sha256 ~ '^[a-f0-9]{64}$'),created_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE(product_id,organization_id,pn,tnved,source_ref))''')
    op.execute('''CREATE TABLE codes(id uuid PRIMARY KEY,product_group text NOT NULL CHECK(product_group='radio'),cis text NOT NULL,
        gtin text NOT NULL CHECK(valid_gtin14(gtin)),product_id uuid,created_at timestamptz NOT NULL DEFAULT now(),
        CHECK(length(cis)=38 AND left(cis,2)='01' AND substr(cis,3,14)=gtin AND substr(cis,17,2)='21'),UNIQUE(product_group,cis),FOREIGN KEY(product_id,gtin) REFERENCES products(id,gtin))''')
    op.execute('''CREATE TABLE code_access(code_id uuid NOT NULL REFERENCES codes(id),organization_id uuid NOT NULL REFERENCES organizations(id),owner_id uuid NOT NULL REFERENCES users(id),
        PRIMARY KEY(code_id,organization_id))''')
    op.execute('''CREATE TABLE code_assignments(code_id uuid NOT NULL,organization_id uuid NOT NULL,user_id uuid NOT NULL REFERENCES users(id),
        PRIMARY KEY(code_id,organization_id,user_id),FOREIGN KEY(code_id,organization_id) REFERENCES code_access(code_id,organization_id))''')
    op.execute('''CREATE TABLE code_payload_versions(id uuid PRIMARY KEY,code_id uuid NOT NULL,organization_id uuid NOT NULL,version bigint NOT NULL CHECK(version>0),
        raw bytea NOT NULL CHECK(octet_length(raw) BETWEEN 1 AND 1024),normalized bytea NOT NULL CHECK(octet_length(normalized) BETWEEN 1 AND 1024),
        raw_sha256 text NOT NULL CHECK(raw_sha256 ~ '^[a-f0-9]{64}$'),normalized_sha256 text NOT NULL CHECK(normalized_sha256 ~ '^[a-f0-9]{64}$'),
        profile text NOT NULL CHECK(profile IN ('radio-strict-v1','radio-scanner-v1')),corrections jsonb NOT NULL,
        template integer NOT NULL CHECK(template IN (36,37)),label_quantity bigint CHECK(label_quantity BETWEEN 1 AND 99999999),
        actor_id uuid NOT NULL REFERENCES users(id),source_ref text NOT NULL CHECK(length(btrim(source_ref))>0),created_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE(code_id,organization_id,version),UNIQUE(id,code_id,organization_id),FOREIGN KEY(code_id,organization_id) REFERENCES code_access(code_id,organization_id))''')
    op.execute('''CREATE TABLE code_events(id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,code_id uuid NOT NULL,organization_id uuid NOT NULL,
        payload_id uuid NOT NULL,kind text NOT NULL CHECK(kind IN ('captured','reobserved','payload_conflict')),
        actor_id uuid NOT NULL REFERENCES users(id),created_at timestamptz NOT NULL DEFAULT now(),
        FOREIGN KEY(payload_id,code_id,organization_id) REFERENCES code_payload_versions(id,code_id,organization_id))''')
    op.execute('''CREATE TABLE files(id uuid PRIMARY KEY,organization_id uuid NOT NULL,code_id uuid NOT NULL,payload_id uuid NOT NULL,
        kind text NOT NULL CHECK(kind IN ('payload','label_pdf')),snapshot jsonb NOT NULL DEFAULT '{}',storage_key text NOT NULL UNIQUE CHECK(storage_key ~ '^[a-f0-9]{2}/[a-f0-9]{32}\\.bin$'),
        sha256 text NOT NULL CHECK(sha256 ~ '^[a-f0-9]{64}$'),size bigint NOT NULL CHECK(size BETWEEN 1 AND 10485760),
        media_type text NOT NULL CHECK((kind='payload' AND media_type='application/octet-stream') OR (kind='label_pdf' AND media_type='application/pdf')),
        created_at timestamptz NOT NULL DEFAULT now(),FOREIGN KEY(payload_id,code_id,organization_id) REFERENCES code_payload_versions(id,code_id,organization_id))''')
    for table in ('products','product_pns','codes','code_payload_versions','code_events','files'):
        op.execute(f'CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON {table} FOR EACH STATEMENT EXECUTE FUNCTION reject_audit_change()')
    op.execute('CREATE INDEX registry_org ON code_access(organization_id,code_id)')
    op.execute('CREATE INDEX code_events_history ON code_events(code_id,organization_id,id DESC)')


def downgrade():
    for table in ('files','code_events','code_payload_versions','code_assignments','code_access','codes','product_pns','products'):op.execute(f'DROP TABLE {table}')
    op.execute('DROP FUNCTION valid_gtin14(text)')

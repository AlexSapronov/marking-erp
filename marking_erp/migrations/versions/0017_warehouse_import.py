"""Explicit BM/Excel warehouse sources and immutable fallback previews."""
from alembic import op
revision='0017_warehouse_import'
down_revision='0016_warehouse_overlap'
branch_labels=None
depends_on=None


def upgrade():
    op.execute('''CREATE TABLE warehouse_imports(
        id uuid PRIMARY KEY,organization_id uuid NOT NULL REFERENCES organizations(id),
        owner_id uuid NOT NULL REFERENCES users(id),kind text NOT NULL CHECK(kind IN ('receipt','shipment')),
        filename text NOT NULL,sha256 char(64) NOT NULL,raw bytea NOT NULL CHECK(octet_length(raw) BETWEEN 1 AND 10485760),
        result jsonb NOT NULL CHECK(jsonb_typeof(result)='object'),created_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE(id,organization_id))''')
    op.execute('CREATE TRIGGER warehouse_imports_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON warehouse_imports FOR EACH STATEMENT EXECUTE FUNCTION reject_audit_change()')
    op.execute('''CREATE TABLE warehouse_sources(
        id uuid PRIMARY KEY,organization_id uuid NOT NULL,kind text NOT NULL CHECK(kind IN ('bm','excel')),
        bm_document_id uuid,import_id uuid,
        FOREIGN KEY(id,organization_id) REFERENCES operation_targets(id,organization_id),
        FOREIGN KEY(bm_document_id,organization_id) REFERENCES bm_documents(id,organization_id),
        FOREIGN KEY(import_id,organization_id) REFERENCES warehouse_imports(id,organization_id),
        CHECK((kind='bm' AND bm_document_id=id AND import_id IS NULL) OR
              (kind='excel' AND import_id=id AND bm_document_id IS NULL)),
        CHECK((kind='bm')=(bm_document_id IS NOT NULL)),CHECK((kind='excel')=(import_id IS NOT NULL)),
        UNIQUE(id,organization_id,kind))''')
    op.execute("INSERT INTO warehouse_sources(id,organization_id,kind,bm_document_id) SELECT id,organization_id,'bm',id FROM bm_documents")
    op.execute('''CREATE FUNCTION register_bm_warehouse_source() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN INSERT INTO warehouse_sources(id,organization_id,kind,bm_document_id) VALUES(NEW.id,NEW.organization_id,'bm',NEW.id);RETURN NEW;END $$''')
    op.execute('CREATE TRIGGER bm_warehouse_source AFTER INSERT ON bm_documents FOR EACH ROW EXECUTE FUNCTION register_bm_warehouse_source()')
    op.execute("ALTER TABLE warehouse_tasks ADD COLUMN source_type text NOT NULL DEFAULT 'bm' CHECK(source_type IN ('bm','excel'))")
    op.execute('ALTER TABLE warehouse_tasks DROP CONSTRAINT warehouse_tasks_source_id_fkey,DROP CONSTRAINT warehouse_tasks_source_id_organization_id_fkey')
    op.execute('ALTER TABLE warehouse_tasks ADD CONSTRAINT warehouse_task_source FOREIGN KEY(source_id,organization_id,source_type) REFERENCES warehouse_sources(id,organization_id,kind)')
    op.execute("CREATE UNIQUE INDEX warehouse_excel_source_once ON warehouse_tasks(source_id) WHERE source_type='excel'")


def downgrade():
    # Older application versions cannot operate Excel-source tasks. Preserve task
    # history when removing this feature; validate old BM FKs after manual review.
    op.execute('DROP INDEX warehouse_excel_source_once')
    op.execute('ALTER TABLE warehouse_tasks DROP CONSTRAINT warehouse_task_source,DROP COLUMN source_type')
    op.execute('ALTER TABLE warehouse_tasks ADD CONSTRAINT warehouse_tasks_source_id_fkey FOREIGN KEY(source_id) REFERENCES bm_documents(id) NOT VALID')
    op.execute('ALTER TABLE warehouse_tasks ADD CONSTRAINT warehouse_tasks_source_id_organization_id_fkey FOREIGN KEY(source_id,organization_id) REFERENCES bm_documents(id,organization_id) NOT VALID')
    op.execute('DROP TRIGGER bm_warehouse_source ON bm_documents')
    op.execute('DROP FUNCTION register_bm_warehouse_source()')
    op.execute('DROP TABLE warehouse_sources,warehouse_imports')

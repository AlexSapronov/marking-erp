"""Frozen warehouse decisions and append-only minimum archive retention."""
from alembic import op
revision='0021_workflow_policy'
down_revision='0020_auto_print'
branch_labels=None
depends_on=None


def upgrade():
    op.execute("ALTER TABLE warehouse_tasks ADD COLUMN policy_snapshot jsonb CHECK(policy_snapshot IS NULL OR jsonb_typeof(policy_snapshot)='object')")
    op.execute("""UPDATE warehouse_tasks SET policy_snapshot=jsonb_build_object('catalog_version',1,'legacy_defaults',true,'values',
        jsonb_build_object('warehouse.missing.warning_threshold',1,'warehouse.duplicate.behavior','error_with_delete_action','warehouse.complete.confirm',true))""")
    op.execute('''CREATE TABLE archive_retention(id uuid PRIMARY KEY,organization_id uuid NOT NULL REFERENCES organizations(id),
        target_id uuid NOT NULL REFERENCES operation_targets(id),actor_id uuid NOT NULL REFERENCES users(id),
        archived_at timestamptz NOT NULL DEFAULT now(),retain_until timestamptz NOT NULL,
        policy_snapshot jsonb NOT NULL,CHECK(retain_until>=archived_at+interval '6 months'))''')
    op.execute('CREATE INDEX archive_retention_target ON archive_retention(organization_id,target_id,retain_until DESC)')
    op.execute('CREATE TRIGGER archive_retention_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON archive_retention FOR EACH STATEMENT EXECUTE FUNCTION reject_audit_change()')


def downgrade():
    op.execute('DROP TABLE archive_retention')
    op.execute('ALTER TABLE warehouse_tasks DROP COLUMN policy_snapshot')

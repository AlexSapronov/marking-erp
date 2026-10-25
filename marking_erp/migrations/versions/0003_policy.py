"""Versioned group changes and organization business policy."""
from alembic import op
revision='0003_policy'
down_revision='0002_auth'
branch_labels=None
depends_on=None


def upgrade():
    op.execute("""CREATE TABLE policy_revisions (
        scope_key text PRIMARY KEY, organization_id uuid UNIQUE REFERENCES organizations(id),
        revision bigint NOT NULL DEFAULT 1 CHECK(revision>0),
        CHECK(scope_key=COALESCE(organization_id::text,'system')) )""")
    op.execute("INSERT INTO policy_revisions(scope_key) VALUES ('system')")
    op.execute("""CREATE TABLE policy_values (
        scope_key text NOT NULL REFERENCES policy_revisions(scope_key),rule_id text NOT NULL,
        value jsonb NOT NULL,PRIMARY KEY(scope_key,rule_id))""")
    op.execute("""CREATE TABLE configuration_changes (
        id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        kind text NOT NULL CHECK(kind IN ('group','rules')),entity text NOT NULL,
        revision bigint NOT NULL CHECK(revision>1),actor_id uuid NOT NULL REFERENCES users(id),
        reason text NOT NULL CHECK(length(btrim(reason)) BETWEEN 8 AND 1000),
        before_value jsonb NOT NULL,after_value jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),UNIQUE(kind,entity,revision))""")
    op.execute("CREATE TRIGGER configuration_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON configuration_changes FOR EACH STATEMENT EXECUTE FUNCTION reject_audit_change()")
    op.execute("""CREATE FUNCTION preserve_admin_grants() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF (TG_OP='DELETE' AND OLD.group_slug='administrator' AND EXISTS(SELECT 1 FROM permissions WHERE id=OLD.permission_id AND id='permissions.manage')) THEN
        RAISE EXCEPTION 'administrator management access is mandatory';
      END IF;
      IF TG_OP<>'DELETE' AND NEW.group_slug='administrator' AND NEW.scope<>'all' AND EXISTS(SELECT 1 FROM permissions WHERE id=NEW.permission_id AND id='permissions.manage') THEN
        RAISE EXCEPTION 'administrator management access is mandatory';
      END IF;
      IF TG_OP='DELETE' THEN RETURN OLD; END IF;
      RETURN NEW;
    END; $$""")
    op.execute("CREATE TRIGGER administrator_grants BEFORE INSERT OR UPDATE OR DELETE ON group_grants FOR EACH ROW EXECUTE FUNCTION preserve_admin_grants()")


def downgrade():
    op.execute('DROP TRIGGER administrator_grants ON group_grants')
    op.execute('DROP FUNCTION preserve_admin_grants()')
    for table in ('configuration_changes','policy_values','policy_revisions'):op.execute(f'DROP TABLE {table}')

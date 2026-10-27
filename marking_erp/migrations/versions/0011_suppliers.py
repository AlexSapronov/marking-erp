"""Organization-scoped supplier aliases, without rewriting document snapshots."""
from alembic import op
revision='0011_suppliers'
down_revision='0010_catalog_sync'
branch_labels=None
depends_on=None


def upgrade():
    op.execute('''CREATE TABLE supplier_aliases(organization_id uuid NOT NULL REFERENCES organizations(id),
        alias text NOT NULL CHECK(length(btrim(alias)) BETWEEN 1 AND 200),
        canonical text NOT NULL CHECK(length(btrim(canonical)) BETWEEN 1 AND 200),
        PRIMARY KEY(organization_id,alias))''')
    op.execute('CREATE INDEX supplier_canonical ON supplier_aliases(organization_id,canonical)')
    op.execute('CREATE TABLE supplier_revisions(organization_id uuid PRIMARY KEY REFERENCES organizations(id),revision bigint NOT NULL CHECK(revision>0))')


def downgrade():op.execute('DROP TABLE supplier_revisions,supplier_aliases')

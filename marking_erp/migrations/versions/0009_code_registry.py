"""Link business allocations to immutable registry payloads."""
from alembic import op

revision='0009_code_registry'
down_revision='0008_business'
branch_labels=None
depends_on=None


def upgrade():
    op.execute('ALTER TABLE code_events DROP CONSTRAINT code_events_kind_check')
    op.execute("ALTER TABLE code_events ADD CONSTRAINT code_events_kind_check CHECK(kind IN ('captured','reobserved','payload_conflict','quantity_adjusted'))")
    op.execute('''ALTER TABLE business_codes
        ADD COLUMN registry_code_id uuid,
        ADD COLUMN registry_payload_id uuid,
        ADD CONSTRAINT business_registry_pair CHECK((registry_code_id IS NULL)=(registry_payload_id IS NULL)),
        ADD CONSTRAINT business_registry_payload FOREIGN KEY(registry_payload_id,registry_code_id,organization_id)
            REFERENCES code_payload_versions(id,code_id,organization_id)''')
    op.execute('CREATE INDEX business_registry_code ON business_codes(registry_code_id,organization_id)')
    op.execute('CREATE INDEX codes_chronological ON codes(created_at DESC,id DESC)')


def downgrade():
    # Keep accepted immutable events; narrowing the CHECK would erase history or
    # fail after a real allocation. The earlier app tolerates additional kinds.
    op.execute('DROP INDEX codes_chronological,business_registry_code')
    op.execute('ALTER TABLE business_codes DROP CONSTRAINT business_registry_payload,DROP CONSTRAINT business_registry_pair,DROP COLUMN registry_payload_id,DROP COLUMN registry_code_id')

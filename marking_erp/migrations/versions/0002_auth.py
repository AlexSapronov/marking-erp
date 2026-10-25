"""Personal sessions, MFA and persistent login throttling."""
from alembic import op

revision = "0002_auth"
down_revision = "0001_core"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE users ADD COLUMN mfa_ciphertext bytea, ADD COLUMN mfa_last_counter bigint NOT NULL DEFAULT -1")
    op.execute("""CREATE TABLE sessions (
        token_hash char(64) PRIMARY KEY, user_id uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
        auth_version bigint NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
        expires_at timestamptz NOT NULL, elevated_at timestamptz,
        CHECK (expires_at > created_at))""")
    op.execute("CREATE INDEX sessions_user ON sessions(user_id)")
    op.execute("CREATE INDEX sessions_expiry ON sessions(expires_at)")
    op.execute("""CREATE TABLE login_limits (
        key char(64) PRIMARY KEY, failures integer NOT NULL CHECK (failures>0),
        window_start timestamptz NOT NULL DEFAULT now())""")


def downgrade():
    op.execute("DROP TABLE login_limits")
    op.execute("DROP TABLE sessions")
    op.execute("ALTER TABLE users DROP COLUMN mfa_ciphertext, DROP COLUMN mfa_last_counter")


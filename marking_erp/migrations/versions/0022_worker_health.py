"""Local worker activity and redacted lifecycle evidence."""
from alembic import op
revision='0022_worker_health'
down_revision='0021_workflow_policy'
branch_labels=None
depends_on=None


def upgrade():
    op.execute('''CREATE TABLE worker_runs(id uuid PRIMARY KEY,build_sha char(40),
        started_at timestamptz NOT NULL DEFAULT clock_timestamp(),heartbeat_at timestamptz,
        thread_alive boolean NOT NULL DEFAULT false,
        state text NOT NULL CHECK(state IN ('starting','running','idle','error','stopped','restored')),
        stage text NOT NULL CHECK(stage IN ('starting','cycle','schedule','auto_print','dispatch','reconcile','catalog_sync','pipeline','token_refresh','idle','stopped')),
        stage_started_at timestamptz NOT NULL DEFAULT clock_timestamp(),cycle_started_at timestamptz,last_success_at timestamptz,
        last_error_at timestamptz,cycles bigint NOT NULL DEFAULT 0 CHECK(cycles>=0),
        errors bigint NOT NULL DEFAULT 0 CHECK(errors>=0),consecutive_errors bigint NOT NULL DEFAULT 0 CHECK(consecutive_errors>=0),
        stopped_at timestamptz)''')
    op.execute('CREATE INDEX worker_latest ON worker_runs(started_at DESC,id DESC)')
    op.execute('CREATE TRIGGER worker_runs_no_delete BEFORE DELETE OR TRUNCATE ON worker_runs FOR EACH STATEMENT EXECUTE FUNCTION reject_audit_change()')
    op.execute('''CREATE TABLE worker_events(id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,worker_id uuid NOT NULL REFERENCES worker_runs(id),
        action text NOT NULL CHECK(action IN ('started','cycle_failed','recovered','stopped','restored')),
        created_at timestamptz NOT NULL DEFAULT clock_timestamp())''')
    op.execute('CREATE TRIGGER worker_events_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON worker_events FOR EACH STATEMENT EXECUTE FUNCTION reject_audit_change()')


def downgrade():
    op.execute('DROP TABLE worker_events,worker_runs')

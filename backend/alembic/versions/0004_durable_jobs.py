"""Frozen durable queue foundation. No worker or external action runs here."""
from alembic import op
import sqlalchemy as sa

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None

DDL = (
    "CREATE TABLE team_job_capacity (\n\tid SERIAL NOT NULL, \n\tpending_count INTEGER DEFAULT '0' NOT NULL, \n\tpending_bytes BIGINT DEFAULT '0' NOT NULL, \n\tactive_count INTEGER DEFAULT '0' NOT NULL, \n\tmax_pending INTEGER DEFAULT '100' NOT NULL, \n\tmax_pending_bytes BIGINT DEFAULT '1073741824' NOT NULL, \n\tgeneration BIGINT DEFAULT '1' NOT NULL, \n\trecovery_hold BOOLEAN DEFAULT false NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT ck_team_job_capacity CHECK (id = 1 AND pending_count >= 0 AND pending_bytes >= 0 AND active_count BETWEEN 0 AND 1 AND max_pending > 0 AND max_pending_bytes > 0 AND generation > 0)\n)",
    "CREATE TABLE team_job_workspace_quota (\n\tworkspace_id VARCHAR(36) NOT NULL, \n\tcommitted_bytes BIGINT DEFAULT '0' NOT NULL, \n\treserved_bytes BIGINT DEFAULT '0' NOT NULL, \n\tlimit_bytes BIGINT DEFAULT '1073741824' NOT NULL, \n\tpending_count INTEGER DEFAULT '0' NOT NULL, \n\tmax_pending INTEGER DEFAULT '20' NOT NULL, \n\tPRIMARY KEY (workspace_id), \n\tCONSTRAINT ck_team_job_quota CHECK (committed_bytes >= 0 AND reserved_bytes >= 0 AND limit_bytes > 0 AND pending_count >= 0 AND max_pending > 0), \n\tFOREIGN KEY(workspace_id) REFERENCES team_workspaces (id)\n)",
    "CREATE TABLE team_jobs (\n\tid VARCHAR(36) NOT NULL, \n\tworkspace_id VARCHAR(36) NOT NULL, \n\tactor_id VARCHAR(36) NOT NULL, \n\tkind VARCHAR(24) NOT NULL, \n\tidempotency_key VARCHAR(128) NOT NULL, \n\trequest_hash VARCHAR(64) NOT NULL, \n\trequest_snapshot JSONB NOT NULL, \n\tstate VARCHAR(24) DEFAULT 'queued' NOT NULL, \n\tattempt INTEGER DEFAULT '0' NOT NULL, \n\tfence_token VARCHAR(36), \n\tworker_id VARCHAR(128), \n\tgeneration BIGINT, \n\tlease_until TIMESTAMP WITH TIME ZONE, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT clock_timestamp() NOT NULL, \n\tfinished_at TIMESTAMP WITH TIME ZONE, \n\treserved_bytes BIGINT NOT NULL, \n\tbase_bytes BIGINT NOT NULL, \n\tretained_bytes BIGINT DEFAULT '0' NOT NULL, \n\tresult JSONB, \n\terror_code VARCHAR(64), \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_team_job_idempotency UNIQUE (workspace_id, actor_id, kind, idempotency_key), \n\tCONSTRAINT uq_team_job_workspace UNIQUE (workspace_id, id), \n\tCONSTRAINT ck_team_job_kind CHECK (kind IN ('import','reconcile','export')), \n\tCONSTRAINT ck_team_job_state CHECK (state IN ('queued','running','cancel_requested','succeeded','failed','cancelled','interrupted')), \n\tCONSTRAINT ck_team_job_request CHECK (jsonb_typeof(request_snapshot) = 'object'), \n\tCONSTRAINT ck_team_job_bytes CHECK (reserved_bytes >= 0 AND base_bytes > 0 AND retained_bytes >= 0 AND attempt >= 0), \n\tFOREIGN KEY(workspace_id) REFERENCES team_workspaces (id), \n\tFOREIGN KEY(actor_id) REFERENCES team_users (id)\n)",
    'CREATE INDEX ix_team_job_queue ON team_jobs (state, created_at, id)',
    'CREATE INDEX ix_team_job_workspace ON team_jobs (workspace_id, created_at, id)',
    "CREATE UNIQUE INDEX uq_team_job_active ON team_jobs ((1)) WHERE state IN ('running','cancel_requested')",
    'CREATE TABLE team_job_attempts (\n\tjob_id VARCHAR(36) NOT NULL, \n\tattempt INTEGER NOT NULL, \n\tfence_token VARCHAR(36) NOT NULL, \n\tworker_id VARCHAR(128) NOT NULL, \n\tstarted_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tfinished_at TIMESTAMP WITH TIME ZONE, \n\tstate VARCHAR(24) NOT NULL, \n\tPRIMARY KEY (job_id, attempt), \n\tFOREIGN KEY(job_id) REFERENCES team_jobs (id), \n\tUNIQUE (fence_token)\n)',
    "CREATE TABLE team_job_quota_ledger (\n\tid VARCHAR(36) NOT NULL, \n\tworkspace_id VARCHAR(36) NOT NULL, \n\tjob_id VARCHAR(36) NOT NULL, \n\tevent VARCHAR(16) NOT NULL, \n\treserved_delta BIGINT NOT NULL, \n\tcommitted_delta BIGINT NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT clock_timestamp() NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(workspace_id, job_id) REFERENCES team_jobs (workspace_id, id), \n\tCONSTRAINT uq_team_job_ledger_event UNIQUE (job_id, event), \n\tCONSTRAINT ck_team_job_ledger_event CHECK (event IN ('reserve','settle'))\n)",
)


def upgrade():
    for statement in DDL:
        op.execute(sa.text(statement))
    op.execute(sa.text('INSERT INTO team_job_capacity(id) VALUES (1)'))
    op.execute(sa.text("""CREATE FUNCTION team_job_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF OLD.state IN ('succeeded','failed','cancelled','interrupted') AND NEW IS DISTINCT FROM OLD THEN
            RAISE EXCEPTION 'terminal job is immutable';
          END IF;
          IF (NEW.workspace_id,NEW.actor_id,NEW.kind,NEW.idempotency_key,NEW.request_hash,NEW.request_snapshot,NEW.base_bytes)
             IS DISTINCT FROM
             (OLD.workspace_id,OLD.actor_id,OLD.kind,OLD.idempotency_key,OLD.request_hash,OLD.request_snapshot,OLD.base_bytes) THEN
            RAISE EXCEPTION 'job request is immutable';
          END IF;
          RETURN NEW;
        END $$"""))
    op.execute(sa.text('CREATE TRIGGER team_job_immutable_update BEFORE UPDATE ON team_jobs FOR EACH ROW EXECUTE FUNCTION team_job_immutable()'))
    op.execute(sa.text('CREATE TRIGGER team_job_ledger_no_update_delete BEFORE UPDATE OR DELETE ON team_job_quota_ledger FOR EACH ROW EXECUTE FUNCTION team_audit_append_only()'))
    op.execute(sa.text('CREATE TRIGGER team_job_ledger_no_truncate BEFORE TRUNCATE ON team_job_quota_ledger FOR EACH STATEMENT EXECUTE FUNCTION team_audit_append_only()'))


def downgrade():
    raise RuntimeError('Restore an isolated verified backup; destructive downgrade unsupported')

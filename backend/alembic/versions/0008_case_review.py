"""Frozen case batches, independent review history and period closing states."""
from alembic import op
import sqlalchemy as sa
revision='0008'
down_revision='0007'
branch_labels=None
depends_on=None

DDL = ('CREATE TABLE team_case_batches (\n\trun_id VARCHAR(36) NOT NULL, \n\tworkspace_id VARCHAR(36) NOT NULL, \n\tperiod_id VARCHAR(36) NOT NULL, \n\tpolicy_version VARCHAR(64) NOT NULL, \n\ttotal_count INTEGER NOT NULL, \n\taccounted_bytes BIGINT NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT clock_timestamp() NOT NULL, \n\tPRIMARY KEY (run_id), \n\tUNIQUE (workspace_id, period_id, run_id), \n\tFOREIGN KEY(workspace_id, period_id, run_id) REFERENCES team_period_runs (workspace_id, period_id, run_id), \n\tCHECK (total_count >= 0 AND accounted_bytes > 0)\n)', "CREATE TABLE team_difference_cases (\n\tid VARCHAR(36) NOT NULL, \n\tworkspace_id VARCHAR(36) NOT NULL, \n\tperiod_id VARCHAR(36) NOT NULL, \n\trun_id VARCHAR(36) NOT NULL, \n\tgroup_id VARCHAR(66) NOT NULL, \n\tkind VARCHAR(32) NOT NULL, \n\tstate VARCHAR(24) DEFAULT 'open' NOT NULL, \n\tversion INTEGER DEFAULT '1' NOT NULL, \n\tassigned_to VARCHAR(36), \n\tsubmitted_by VARCHAR(36), \n\treviewed_by VARCHAR(36), \n\tresolution_json JSONB, \n\tresolution_hash VARCHAR(64), \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT clock_timestamp() NOT NULL, \n\tupdated_at TIMESTAMP WITH TIME ZONE DEFAULT clock_timestamp() NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (workspace_id, id), \n\tUNIQUE (workspace_id, run_id, group_id), \n\tFOREIGN KEY(workspace_id, period_id, run_id) REFERENCES team_case_batches (workspace_id, period_id, run_id), \n\tFOREIGN KEY(workspace_id, run_id, group_id) REFERENCES team_collection_groups (workspace_id, run_id, group_id), \n\tCHECK (version > 0), \n\tCHECK (state IN ('open','in_progress','pending_review','closed')), \n\tCHECK (kind <> 'exact_equal'), \n\tCHECK (state='open' OR assigned_to IS NOT NULL), \n\tCHECK (state NOT IN ('pending_review','closed') OR (submitted_by IS NOT NULL AND resolution_json IS NOT NULL AND resolution_hash IS NOT NULL)), \n\tCHECK (state<>'closed' OR (reviewed_by IS NOT NULL AND reviewed_by<>submitted_by)), \n\tCHECK ((resolution_json IS NULL)=(resolution_hash IS NULL)), \n\tFOREIGN KEY(assigned_to) REFERENCES team_users (id), \n\tFOREIGN KEY(submitted_by) REFERENCES team_users (id), \n\tFOREIGN KEY(reviewed_by) REFERENCES team_users (id)\n)", 'CREATE INDEX ix_team_case_period_run ON team_difference_cases (workspace_id, period_id, run_id, state, id)', 'CREATE TABLE team_case_events (\n\tid VARCHAR(36) NOT NULL, \n\tworkspace_id VARCHAR(36) NOT NULL, \n\tcase_id VARCHAR(36) NOT NULL, \n\tsequence INTEGER NOT NULL, \n\taction VARCHAR(32) NOT NULL, \n\tactor_id VARCHAR(36), \n\tdetail JSONB NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT clock_timestamp() NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(workspace_id, case_id) REFERENCES team_difference_cases (workspace_id, id), \n\tUNIQUE (case_id, sequence), \n\tCHECK (sequence > 0), \n\tFOREIGN KEY(actor_id) REFERENCES team_users (id)\n)')


def upgrade():
    for statement in DDL:op.execute(sa.text(statement))
    for name in ['team_case_batches','team_case_events']:
        op.execute(sa.text(f'CREATE TRIGGER {name}_immutable BEFORE UPDATE OR DELETE ON {name} FOR EACH ROW EXECUTE FUNCTION team_source_immutable()'))
        op.execute(sa.text(f'CREATE TRIGGER {name}_no_truncate BEFORE TRUNCATE ON {name} FOR EACH STATEMENT EXECUTE FUNCTION team_source_immutable()'))
    op.execute(sa.text("""CREATE FUNCTION team_case_guard() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
        IF TG_OP='DELETE' THEN RAISE EXCEPTION 'case history is immutable'; END IF;
        IF NEW.id IS DISTINCT FROM OLD.id OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
           OR NEW.period_id IS DISTINCT FROM OLD.period_id OR NEW.run_id IS DISTINCT FROM OLD.run_id
           OR NEW.group_id IS DISTINCT FROM OLD.group_id OR NEW.kind IS DISTINCT FROM OLD.kind
           OR NEW.created_at IS DISTINCT FROM OLD.created_at OR NEW.version <> OLD.version + 1 THEN
            RAISE EXCEPTION 'case identity is immutable; changes require next version';
        END IF;
        IF NOT ((OLD.state='open' AND NEW.state='in_progress')
                OR (OLD.state='in_progress' AND NEW.state='pending_review')
                OR (OLD.state='pending_review' AND NEW.state IN ('in_progress','closed'))
                OR (OLD.state='closed' AND NEW.state='in_progress')) THEN
            RAISE EXCEPTION 'invalid case workflow transition';
        END IF;
        IF OLD.state IN ('pending_review','closed') AND (NEW.resolution_json IS DISTINCT FROM OLD.resolution_json
            OR NEW.resolution_hash IS DISTINCT FROM OLD.resolution_hash OR NEW.submitted_by IS DISTINCT FROM OLD.submitted_by) THEN
            RAISE EXCEPTION 'submitted case evidence is immutable';
        END IF;
        IF OLD.assigned_to IS NOT NULL AND NEW.assigned_to IS DISTINCT FROM OLD.assigned_to THEN
            RAISE EXCEPTION 'case assignment is immutable';
        END IF;
        RETURN NEW;
    END $$"""))
    op.execute(sa.text('CREATE TRIGGER team_case_guard BEFORE UPDATE OR DELETE ON team_difference_cases FOR EACH ROW EXECUTE FUNCTION team_case_guard()'))
    op.execute(sa.text('CREATE TRIGGER team_case_no_truncate BEFORE TRUNCATE ON team_difference_cases FOR EACH STATEMENT EXECUTE FUNCTION team_source_immutable()'))
    # 0007 includes unnamed checks; identify only the period state constraints,
    # preserving identity/version and foreign-key constraints on upgraded data.
    op.execute(sa.text("""DO $$ DECLARE item record; BEGIN
        FOR item IN SELECT conname FROM pg_constraint WHERE conrelid='team_business_periods'::regclass
          AND contype='c' AND pg_get_constraintdef(oid) LIKE '%state%' LOOP
            EXECUTE format('ALTER TABLE team_business_periods DROP CONSTRAINT %I',item.conname);
        END LOOP;
    END $$"""))
    op.execute(sa.text("ALTER TABLE team_business_periods ADD CONSTRAINT ck_period_state CHECK (state IN ('draft','ready','reviewing','closed'))"))
    op.execute(sa.text("ALTER TABLE team_business_periods ADD CONSTRAINT ck_period_confirmation_present CHECK (state='draft' OR confirmed_revision_id IS NOT NULL)"))
    op.execute(sa.text("ALTER TABLE team_business_periods ADD CONSTRAINT ck_period_review_active CHECK (state NOT IN ('reviewing','closed') OR active_run_id IS NOT NULL)"))
    op.execute(sa.text("ALTER TABLE team_business_periods ADD CONSTRAINT ck_period_current_confirmation CHECK ((state='draft' AND confirmed_revision_id IS NULL) OR (state IN ('ready','reviewing','closed') AND current_revision_id IS NOT NULL AND confirmed_revision_id=current_revision_id))"))


def downgrade():
    raise RuntimeError('Restore a verified isolated backup')

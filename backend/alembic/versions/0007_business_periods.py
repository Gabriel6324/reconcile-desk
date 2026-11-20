"""Frozen reusable profiles and period input/activation history."""
from alembic import op
import sqlalchemy as sa
revision='0007'
down_revision='0006'
branch_labels=None
depends_on=None

DDL = ('CREATE TABLE team_collection_profiles (\n\tid VARCHAR(36) NOT NULL, \n\tworkspace_id VARCHAR(36) NOT NULL, \n\tsource_rule_id VARCHAR(36) NOT NULL, \n\tname VARCHAR(120) NOT NULL, \n\tdefinition_json JSONB NOT NULL, \n\tprofile_hash VARCHAR(64) NOT NULL, \n\tcreated_by VARCHAR(36) NOT NULL, \n\taccounted_bytes BIGINT NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT clock_timestamp() NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (workspace_id, id), \n\tFOREIGN KEY(workspace_id, source_rule_id) REFERENCES team_collection_rules (workspace_id, id), \n\tCHECK (accounted_bytes > 0), \n\tFOREIGN KEY(workspace_id) REFERENCES team_workspaces (id), \n\tFOREIGN KEY(created_by) REFERENCES team_users (id)\n)', "CREATE TABLE team_business_periods (\n\tid VARCHAR(36) NOT NULL, \n\tworkspace_id VARCHAR(36) NOT NULL, \n\tdefinition_json JSONB NOT NULL, \n\tdefinition_hash VARCHAR(64) NOT NULL, \n\tversion INTEGER DEFAULT '1' NOT NULL, \n\trevision_number INTEGER DEFAULT '0' NOT NULL, \n\tcurrent_revision_id VARCHAR(36), \n\tconfirmed_revision_id VARCHAR(36), \n\tactive_run_id VARCHAR(36), \n\tstate VARCHAR(16) DEFAULT 'draft' NOT NULL, \n\tcreated_by VARCHAR(36) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT clock_timestamp() NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (workspace_id, id), \n\tCHECK (version > 0 AND revision_number >= 0), \n\tCHECK (state IN ('draft','ready','reviewing')), \n\tCHECK ((state='draft' AND confirmed_revision_id IS NULL) OR (state IN ('ready','reviewing') AND current_revision_id IS NOT NULL AND confirmed_revision_id=current_revision_id)), \n\tFOREIGN KEY(workspace_id) REFERENCES team_workspaces (id), \n\tFOREIGN KEY(created_by) REFERENCES team_users (id)\n)", 'CREATE TABLE team_period_revisions (\n\tid VARCHAR(36) NOT NULL, \n\tworkspace_id VARCHAR(36) NOT NULL, \n\tperiod_id VARCHAR(36) NOT NULL, \n\tnumber INTEGER NOT NULL, \n\tprofile_id VARCHAR(36) NOT NULL, \n\trule_id VARCHAR(36) NOT NULL, \n\tconfig_json JSONB NOT NULL, \n\tconfig_hash VARCHAR(64) NOT NULL, \n\tcreated_by VARCHAR(36) NOT NULL, \n\taccounted_bytes BIGINT NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT clock_timestamp() NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (workspace_id, period_id, id), \n\tUNIQUE (period_id, number), \n\tFOREIGN KEY(workspace_id, period_id) REFERENCES team_business_periods (workspace_id, id), \n\tFOREIGN KEY(workspace_id, profile_id) REFERENCES team_collection_profiles (workspace_id, id), \n\tFOREIGN KEY(workspace_id, rule_id) REFERENCES team_collection_rules (workspace_id, id), \n\tCHECK (number > 0 AND accounted_bytes > 0), \n\tFOREIGN KEY(created_by) REFERENCES team_users (id)\n)', 'CREATE TABLE team_period_runs (\n\trun_id VARCHAR(36) NOT NULL, \n\tworkspace_id VARCHAR(36) NOT NULL, \n\tperiod_id VARCHAR(36) NOT NULL, \n\trevision_id VARCHAR(36) NOT NULL, \n\tconfig_hash VARCHAR(64) NOT NULL, \n\tsubmitted_by VARCHAR(36) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT clock_timestamp() NOT NULL, \n\tPRIMARY KEY (run_id), \n\tUNIQUE (workspace_id, period_id, run_id), \n\tFOREIGN KEY(workspace_id, period_id, revision_id) REFERENCES team_period_revisions (workspace_id, period_id, id), \n\tFOREIGN KEY(workspace_id, run_id) REFERENCES team_collection_runs (workspace_id, id), \n\tFOREIGN KEY(submitted_by) REFERENCES team_users (id)\n)', 'ALTER TABLE team_business_periods ADD CONSTRAINT fk_period_active_run FOREIGN KEY(workspace_id, id, active_run_id) REFERENCES team_period_runs (workspace_id, period_id, run_id)', 'ALTER TABLE team_business_periods ADD CONSTRAINT fk_period_confirmed_revision FOREIGN KEY(workspace_id, id, confirmed_revision_id) REFERENCES team_period_revisions (workspace_id, period_id, id)', 'ALTER TABLE team_business_periods ADD CONSTRAINT fk_period_current_revision FOREIGN KEY(workspace_id, id, current_revision_id) REFERENCES team_period_revisions (workspace_id, period_id, id)')

def upgrade():
    for statement in DDL:op.execute(sa.text(statement))
    op.execute(sa.text("ALTER TABLE team_business_periods ADD CONSTRAINT ck_period_confirmation_present CHECK (state='draft' OR confirmed_revision_id IS NOT NULL)"))
    op.execute(sa.text("ALTER TABLE team_business_periods ADD CONSTRAINT ck_period_review_active CHECK (state<>'reviewing' OR active_run_id IS NOT NULL)"))
    for name in ['team_collection_profiles','team_period_revisions','team_period_runs']:
        op.execute(sa.text(f'CREATE TRIGGER {name}_immutable BEFORE UPDATE OR DELETE ON {name} FOR EACH ROW EXECUTE FUNCTION team_source_immutable()'))
        op.execute(sa.text(f'CREATE TRIGGER {name}_no_truncate BEFORE TRUNCATE ON {name} FOR EACH STATEMENT EXECUTE FUNCTION team_source_immutable()'))
    op.execute(sa.text("""CREATE FUNCTION team_period_guard() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
        IF TG_OP='DELETE' THEN RAISE EXCEPTION 'period history is immutable'; END IF;
        IF NEW.id IS DISTINCT FROM OLD.id OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
           OR NEW.definition_json IS DISTINCT FROM OLD.definition_json OR NEW.definition_hash IS DISTINCT FROM OLD.definition_hash
           OR NEW.created_by IS DISTINCT FROM OLD.created_by OR NEW.created_at IS DISTINCT FROM OLD.created_at
           OR NEW.version <> OLD.version + 1 OR NEW.revision_number < OLD.revision_number THEN
            RAISE EXCEPTION 'period identity is immutable; updates require next version';
        END IF; RETURN NEW; END $$"""))
    op.execute(sa.text('CREATE TRIGGER team_period_guard BEFORE UPDATE OR DELETE ON team_business_periods FOR EACH ROW EXECUTE FUNCTION team_period_guard()'))
    op.execute(sa.text('CREATE TRIGGER team_period_no_truncate BEFORE TRUNCATE ON team_business_periods FOR EACH STATEMENT EXECUTE FUNCTION team_source_immutable()'))


def downgrade():
    raise RuntimeError('Restore a verified isolated backup')

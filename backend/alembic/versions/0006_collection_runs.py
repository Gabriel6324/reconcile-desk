"""Frozen immutable collection rule, run and result schema."""
from alembic import op
import sqlalchemy as sa
revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None

DDL = ('CREATE TABLE team_collection_rules (\n\tid VARCHAR(36) NOT NULL, \n\tworkspace_id VARCHAR(36) NOT NULL, \n\tname VARCHAR(120) NOT NULL, \n\tpublished_by VARCHAR(36) NOT NULL, \n\tleft_dataset_id VARCHAR(36) NOT NULL, \n\tright_dataset_id VARCHAR(36) NOT NULL, \n\tdefinition_json JSONB NOT NULL, \n\trule_hash VARCHAR(64) NOT NULL, \n\taccounted_bytes BIGINT NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT clock_timestamp() NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (workspace_id, id), \n\tFOREIGN KEY(workspace_id, left_dataset_id) REFERENCES team_datasets (workspace_id, id), \n\tFOREIGN KEY(workspace_id, right_dataset_id) REFERENCES team_datasets (workspace_id, id), \n\tCHECK (left_dataset_id <> right_dataset_id AND accounted_bytes > 0), \n\tFOREIGN KEY(workspace_id) REFERENCES team_workspaces (id), \n\tFOREIGN KEY(published_by) REFERENCES team_users (id)\n)', 'CREATE TABLE team_collection_runs (\n\tid VARCHAR(36) NOT NULL, \n\tworkspace_id VARCHAR(36) NOT NULL, \n\trule_id VARCHAR(36) NOT NULL, \n\tjob_id VARCHAR(36), \n\tinput_hash VARCHAR(64) NOT NULL, \n\tresult_hash VARCHAR(64), \n\tsummary_json JSONB, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT clock_timestamp() NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (workspace_id, id), \n\tFOREIGN KEY(workspace_id, rule_id) REFERENCES team_collection_rules (workspace_id, id), \n\tFOREIGN KEY(workspace_id, job_id) REFERENCES team_jobs (workspace_id, id), \n\tFOREIGN KEY(workspace_id) REFERENCES team_workspaces (id), \n\tUNIQUE (job_id)\n)', 'CREATE TABLE team_collection_groups (\n\tworkspace_id VARCHAR(36) NOT NULL, \n\trun_id VARCHAR(36) NOT NULL, \n\tgroup_id VARCHAR(66) NOT NULL, \n\tordinal INTEGER NOT NULL, \n\tdetail JSONB NOT NULL, \n\tPRIMARY KEY (workspace_id, run_id, group_id), \n\tFOREIGN KEY(workspace_id, run_id) REFERENCES team_collection_runs (workspace_id, id), \n\tUNIQUE (run_id, ordinal), \n\tCHECK (ordinal > 0)\n)', "CREATE TABLE team_collection_coverage (\n\tworkspace_id VARCHAR(36) NOT NULL, \n\trun_id VARCHAR(36) NOT NULL, \n\tside VARCHAR(8) NOT NULL, \n\tdataset_id VARCHAR(36) NOT NULL, \n\trow_id VARCHAR(32) NOT NULL, \n\tgroup_id VARCHAR(66) NOT NULL, \n\tdisposition VARCHAR(32) NOT NULL, \n\tPRIMARY KEY (workspace_id, run_id, side, dataset_id, row_id), \n\tFOREIGN KEY(workspace_id, run_id, group_id) REFERENCES team_collection_groups (workspace_id, run_id, group_id), \n\tFOREIGN KEY(workspace_id, dataset_id, row_id) REFERENCES team_dataset_rows (workspace_id, dataset_id, row_id), \n\tCHECK (side IN ('left','right'))\n)")

def upgrade():
    for statement in DDL:
        op.execute(sa.text(statement))
    for name in ['team_collection_rules', 'team_collection_groups', 'team_collection_coverage']:
        op.execute(sa.text(f'CREATE TRIGGER {name}_immutable BEFORE UPDATE OR DELETE ON {name} FOR EACH ROW EXECUTE FUNCTION team_source_immutable()'))
        op.execute(sa.text(f'CREATE TRIGGER {name}_no_truncate BEFORE TRUNCATE ON {name} FOR EACH STATEMENT EXECUTE FUNCTION team_source_immutable()'))
    op.execute(sa.text("""CREATE FUNCTION team_collection_run_guard() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
        IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'collection run evidence is immutable'; END IF;
        IF OLD.result_hash IS NOT NULL OR NEW.id IS DISTINCT FROM OLD.id OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
           OR NEW.rule_id IS DISTINCT FROM OLD.rule_id OR NEW.input_hash IS DISTINCT FROM OLD.input_hash
           OR NEW.created_at IS DISTINCT FROM OLD.created_at
           OR (OLD.job_id IS NOT NULL AND NEW.job_id IS DISTINCT FROM OLD.job_id) THEN
           RAISE EXCEPTION 'collection run evidence is immutable';
        END IF; RETURN NEW; END $$"""))
    op.execute(sa.text('CREATE TRIGGER team_collection_run_guard BEFORE UPDATE OR DELETE ON team_collection_runs FOR EACH ROW EXECUTE FUNCTION team_collection_run_guard()'))
    op.execute(sa.text('CREATE TRIGGER team_collection_runs_no_truncate BEFORE TRUNCATE ON team_collection_runs FOR EACH STATEMENT EXECUTE FUNCTION team_source_immutable()'))


def downgrade():
    raise RuntimeError('Restore a verified isolated backup')

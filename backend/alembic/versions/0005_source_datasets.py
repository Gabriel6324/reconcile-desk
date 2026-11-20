"""Frozen source asset and dataset schema."""
from alembic import op
import sqlalchemy as sa
revision='0005'
down_revision='0004'
branch_labels=None
depends_on=None

DDL=('CREATE TABLE team_source_assets (\n\tid VARCHAR(36) NOT NULL, \n\tworkspace_id VARCHAR(36) NOT NULL, \n\tcreated_by VARCHAR(36) NOT NULL, \n\tfilename VARCHAR(240) NOT NULL, \n\tsha256 VARCHAR(64) NOT NULL, \n\tcontent BYTEA NOT NULL, \n\taccounted_bytes BIGINT NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT clock_timestamp() NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (workspace_id, id), \n\tCHECK (accounted_bytes >= octet_length(content)), \n\tFOREIGN KEY(workspace_id) REFERENCES team_workspaces (id), \n\tFOREIGN KEY(created_by) REFERENCES team_users (id)\n)',"CREATE TABLE team_datasets (\n\tid VARCHAR(36) NOT NULL, \n\tworkspace_id VARCHAR(36) NOT NULL, \n\tasset_id VARCHAR(36) NOT NULL, \n\tjob_id VARCHAR(36), \n\tworksheet VARCHAR(240), \n\tmetadata_json JSONB, \n\tparsed_hash VARCHAR(64), \n\terror_details JSONB, \n\trow_count INTEGER DEFAULT '0' NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE DEFAULT clock_timestamp() NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (workspace_id, id), \n\tFOREIGN KEY(workspace_id, asset_id) REFERENCES team_source_assets (workspace_id, id), \n\tFOREIGN KEY(workspace_id, job_id) REFERENCES team_jobs (workspace_id, id), \n\tCHECK (row_count >= 0), \n\tFOREIGN KEY(workspace_id) REFERENCES team_workspaces (id), \n\tUNIQUE (job_id)\n)",'CREATE TABLE team_dataset_rows (\n\tworkspace_id VARCHAR(36) NOT NULL, \n\tdataset_id VARCHAR(36) NOT NULL, \n\trow_id VARCHAR(32) NOT NULL, \n\tordinal INTEGER NOT NULL, \n\tsource JSONB NOT NULL, \n\tcells JSONB NOT NULL, \n\tPRIMARY KEY (workspace_id, dataset_id, row_id), \n\tFOREIGN KEY(workspace_id, dataset_id) REFERENCES team_datasets (workspace_id, id), \n\tUNIQUE (dataset_id, ordinal), \n\tCHECK (ordinal > 0)\n)',)

def upgrade():
    op.add_column('team_jobs',sa.Column('external_retained_bytes',sa.BigInteger(),server_default='0',nullable=False))
    op.create_check_constraint('ck_team_external_retained','team_jobs','external_retained_bytes >= 0')
    for statement in DDL:
        op.execute(sa.text(statement))
    op.execute(sa.text("CREATE FUNCTION team_source_immutable() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'source evidence is immutable'; END $$"))
    for name in ['team_source_assets','team_dataset_rows']:
        op.execute(sa.text(f'CREATE TRIGGER {name}_immutable BEFORE UPDATE OR DELETE ON {name} FOR EACH ROW EXECUTE FUNCTION team_source_immutable()'))
        op.execute(sa.text(f'CREATE TRIGGER {name}_no_truncate BEFORE TRUNCATE ON {name} FOR EACH STATEMENT EXECUTE FUNCTION team_source_immutable()'))
    op.execute(sa.text("""CREATE FUNCTION team_dataset_guard() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
      IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'dataset evidence is immutable'; END IF;
      IF OLD.parsed_hash IS NOT NULL OR NEW.id IS DISTINCT FROM OLD.id OR NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
         OR NEW.asset_id IS DISTINCT FROM OLD.asset_id OR NEW.worksheet IS DISTINCT FROM OLD.worksheet
         OR (OLD.job_id IS NOT NULL AND NEW.job_id IS DISTINCT FROM OLD.job_id) THEN
         RAISE EXCEPTION 'dataset evidence is immutable';
      END IF; RETURN NEW; END $$"""))
    op.execute(sa.text('CREATE TRIGGER team_dataset_guard BEFORE UPDATE OR DELETE ON team_datasets FOR EACH ROW EXECUTE FUNCTION team_dataset_guard()'))

def downgrade():
    raise RuntimeError('Restore a verified isolated backup')

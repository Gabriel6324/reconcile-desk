from alembic import op
import sqlalchemy as sa
revision='0009'
down_revision='0008'
branch_labels=None
depends_on=None

def upgrade():
    op.execute(sa.text('''CREATE TABLE team_period_reports (
        id varchar(36) PRIMARY KEY, workspace_id varchar(36) NOT NULL, period_id varchar(36) NOT NULL,
        period_version integer NOT NULL, run_id varchar(36) NOT NULL, sha256 varchar(64) NOT NULL,
        content bytea NOT NULL, accounted_bytes bigint NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
        UNIQUE(period_id,period_version),FOREIGN KEY(workspace_id,period_id) REFERENCES team_business_periods(workspace_id,id),
        FOREIGN KEY(workspace_id,run_id) REFERENCES team_collection_runs(workspace_id,id))'''))
    op.execute(sa.text('CREATE TRIGGER team_reports_immutable BEFORE UPDATE OR DELETE ON team_period_reports FOR EACH ROW EXECUTE FUNCTION team_source_immutable()'))
    op.execute(sa.text('CREATE TRIGGER team_reports_no_truncate BEFORE TRUNCATE ON team_period_reports FOR EACH STATEMENT EXECUTE FUNCTION team_source_immutable()'))

def downgrade():raise RuntimeError('Restore a verified isolated backup')

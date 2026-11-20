"""Frozen identity and workspace foundation; explicit opt-in team migration."""
from alembic import op
import sqlalchemy as sa
revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None

DDL = (
    "CREATE TABLE team_login_throttles (\n\tkey_hash VARCHAR(64) NOT NULL, \n\tfailures INTEGER DEFAULT '0' NOT NULL, \n\twindow_start TIMESTAMP WITH TIME ZONE NOT NULL, \n\tblocked_until TIMESTAMP WITH TIME ZONE, \n\tPRIMARY KEY (key_hash), \n\tCONSTRAINT ck_team_login_failures CHECK (failures >= 0)\n)",
    'CREATE INDEX ix_team_login_window ON team_login_throttles (window_start)',
    'CREATE TABLE team_organizations (\n\tid VARCHAR(36) NOT NULL, \n\tname VARCHAR(160) NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT ck_team_org_name CHECK (char_length(name) BETWEEN 1 AND 160)\n)',
    "CREATE TABLE team_users (\n\tid VARCHAR(36) NOT NULL, \n\tlogin VARCHAR(128) NOT NULL, \n\tpassword_hash VARCHAR(512) NOT NULL, \n\tactive BOOLEAN DEFAULT true NOT NULL, \n\tsession_version INTEGER DEFAULT '1' NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT ck_team_user_login CHECK (char_length(login) BETWEEN 3 AND 128), \n\tCONSTRAINT ck_team_user_session_version CHECK (session_version >= 1), \n\tUNIQUE (login)\n)",
    "CREATE TABLE team_mutation_receipts (\n\tscope_id VARCHAR(64) NOT NULL, \n\tactor_id VARCHAR(36) NOT NULL, \n\taction VARCHAR(80) NOT NULL, \n\tkey VARCHAR(128) NOT NULL, \n\trequest_hash VARCHAR(64) NOT NULL, \n\tresult JSONB NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (scope_id, actor_id, action, key), \n\tCONSTRAINT ck_team_receipt_key CHECK (char_length(key) BETWEEN 1 AND 128), \n\tCONSTRAINT ck_team_receipt_result CHECK (jsonb_typeof(result) = 'object'), \n\tFOREIGN KEY(actor_id) REFERENCES team_users (id)\n)",
    "CREATE TABLE team_organization_roles (\n\torg_id VARCHAR(36) NOT NULL, \n\tuser_id VARCHAR(36) NOT NULL, \n\trole VARCHAR(32) NOT NULL, \n\tPRIMARY KEY (org_id, user_id, role), \n\tCONSTRAINT ck_team_org_role CHECK (role = 'deployment_admin'), \n\tFOREIGN KEY(org_id) REFERENCES team_organizations (id), \n\tFOREIGN KEY(user_id) REFERENCES team_users (id)\n)",
    'CREATE TABLE team_sessions (\n\ttoken_hash VARCHAR(64) NOT NULL, \n\tuser_id VARCHAR(36) NOT NULL, \n\tcsrf_hash VARCHAR(64) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tlast_seen_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\texpires_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tsession_version INTEGER NOT NULL, \n\tPRIMARY KEY (token_hash), \n\tCONSTRAINT ck_team_session_version CHECK (session_version >= 1), \n\tCONSTRAINT ck_team_session_dates CHECK (expires_at > created_at AND last_seen_at >= created_at), \n\tFOREIGN KEY(user_id) REFERENCES team_users (id)\n)',
    'CREATE INDEX ix_team_session_expiry ON team_sessions (expires_at)',
    'CREATE INDEX ix_team_session_user ON team_sessions (user_id)',
    "CREATE TABLE team_workspaces (\n\tid VARCHAR(36) NOT NULL, \n\torg_id VARCHAR(36) NOT NULL, \n\tname VARCHAR(160) NOT NULL, \n\tversion INTEGER DEFAULT '1' NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT uq_team_workspace_org UNIQUE (id, org_id), \n\tCONSTRAINT uq_team_workspace_name UNIQUE (org_id, name), \n\tCONSTRAINT ck_team_workspace_version CHECK (version >= 1), \n\tCONSTRAINT ck_team_workspace_name CHECK (char_length(name) BETWEEN 1 AND 160), \n\tFOREIGN KEY(org_id) REFERENCES team_organizations (id)\n)",
    "CREATE TABLE team_audit_events (\n\tid VARCHAR(36) NOT NULL, \n\torg_id VARCHAR(36) NOT NULL, \n\tworkspace_id VARCHAR(36), \n\tactor_id VARCHAR(36), \n\taction VARCHAR(80) NOT NULL, \n\tresource_type VARCHAR(48) NOT NULL, \n\tresource_id VARCHAR(128), \n\trequest_id VARCHAR(64) NOT NULL, \n\tsafe_detail JSONB DEFAULT '{}'::jsonb NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (id), \n\tCONSTRAINT fk_team_audit_workspace_org FOREIGN KEY(workspace_id, org_id) REFERENCES team_workspaces (id, org_id), \n\tCONSTRAINT ck_team_audit_detail CHECK (jsonb_typeof(safe_detail) = 'object'), \n\tFOREIGN KEY(org_id) REFERENCES team_organizations (id), \n\tFOREIGN KEY(actor_id) REFERENCES team_users (id)\n)",
    'CREATE INDEX ix_team_audit_org_time ON team_audit_events (org_id, created_at, id)',
    'CREATE INDEX ix_team_audit_workspace_time ON team_audit_events (workspace_id, created_at, id)',
    'CREATE TABLE team_legacy_scopes (\n\tresource_type VARCHAR(32) NOT NULL, \n\tlegacy_id VARCHAR(36) NOT NULL, \n\tworkspace_id VARCHAR(36) NOT NULL, \n\tPRIMARY KEY (resource_type, legacy_id), \n\tFOREIGN KEY(workspace_id) REFERENCES team_workspaces (id)\n)',
    'CREATE INDEX ix_team_legacy_scope_workspace ON team_legacy_scopes (workspace_id, resource_type)',
    'CREATE TABLE team_memberships (\n\tworkspace_id VARCHAR(36) NOT NULL, \n\tuser_id VARCHAR(36) NOT NULL, \n\troles JSONB NOT NULL, \n\tactive BOOLEAN DEFAULT true NOT NULL, \n\tversion INTEGER DEFAULT \'1\' NOT NULL, \n\tPRIMARY KEY (workspace_id, user_id), \n\tCONSTRAINT ck_team_membership_version CHECK (version >= 1), \n\tCONSTRAINT ck_team_membership_roles CHECK (jsonb_typeof(roles) = \'array\' AND jsonb_array_length(roles) BETWEEN 1 AND 4 AND roles <@ \'["workspace_manager","operator","reviewer","viewer"]\'::jsonb), \n\tFOREIGN KEY(workspace_id) REFERENCES team_workspaces (id), \n\tFOREIGN KEY(user_id) REFERENCES team_users (id)\n)',
    'CREATE INDEX ix_team_membership_user ON team_memberships (user_id, active)',
)

def upgrade():
    for statement in DDL:
        op.execute(sa.text(statement))
    op.execute(sa.text("INSERT INTO team_organizations(id,name) VALUES ('00000000-0000-0000-0000-000000000001','Reconcile Desk')"))
    op.execute(sa.text("INSERT INTO team_workspaces(id,org_id,name,version) VALUES ('00000000-0000-0000-0000-000000000002','00000000-0000-0000-0000-000000000001','Legacy workspace',1)"))
    for table, kind in [('imports','import'), ('runs','run'), ('mappings','mapping'), ('mapping_templates','template'), ('exports','export'), ('operations','operation'), ('model_calls','suggestion')]:
        op.execute(sa.text(f"INSERT INTO team_legacy_scopes(resource_type,legacy_id,workspace_id) SELECT '{kind}',id,'00000000-0000-0000-0000-000000000002' FROM {table}"))
    op.execute(sa.text("""CREATE FUNCTION team_audit_append_only() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'audit events are append only'; END $$"""))
    op.execute(sa.text("CREATE TRIGGER team_audit_no_update_delete BEFORE UPDATE OR DELETE ON team_audit_events FOR EACH ROW EXECUTE FUNCTION team_audit_append_only()"))
    op.execute(sa.text("CREATE TRIGGER team_audit_no_truncate BEFORE TRUNCATE ON team_audit_events FOR EACH STATEMENT EXECUTE FUNCTION team_audit_append_only()"))

def downgrade():
    raise RuntimeError('Restore an isolated verified backup; destructive downgrade unsupported')

"""Identity and resource scope for the team service.

Separate metadata prevents the legacy initial migration from creating new tables.
All timestamps are UTC and all schema mutations belong to explicit migrations.
"""
from datetime import datetime

from sqlalchemy import (
    Boolean, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint,
    Index, Integer, String, UniqueConstraint, text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from api.db import new_id, now


class TeamBase(DeclarativeBase):
    pass


class User(TeamBase):
    __tablename__ = 'team_users'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    login: Mapped[str] = mapped_column(String(128), unique=True)
    password_hash: Mapped[str] = mapped_column(String(512))
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text('true'))
    session_version: Mapped[int] = mapped_column(Integer, default=1, server_default='1')
    __table_args__ = (
        CheckConstraint('char_length(login) BETWEEN 3 AND 128', name='ck_team_user_login'),
        CheckConstraint('session_version >= 1', name='ck_team_user_session_version'),
    )


class Organization(TeamBase):
    __tablename__ = 'team_organizations'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(160))
    __table_args__ = (CheckConstraint('char_length(name) BETWEEN 1 AND 160', name='ck_team_org_name'),)


class OrganizationRole(TeamBase):
    __tablename__ = 'team_organization_roles'
    org_id: Mapped[str] = mapped_column(ForeignKey('team_organizations.id'), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey('team_users.id'), primary_key=True)
    role: Mapped[str] = mapped_column(String(32), primary_key=True)
    __table_args__ = (CheckConstraint("role = 'deployment_admin'", name='ck_team_org_role'),)


class Workspace(TeamBase):
    __tablename__ = 'team_workspaces'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(ForeignKey('team_organizations.id'))
    name: Mapped[str] = mapped_column(String(160))
    version: Mapped[int] = mapped_column(Integer, default=1, server_default='1')
    __table_args__ = (
        UniqueConstraint('id', 'org_id', name='uq_team_workspace_org'),
        UniqueConstraint('org_id', 'name', name='uq_team_workspace_name'),
        CheckConstraint('version >= 1', name='ck_team_workspace_version'),
        CheckConstraint('char_length(name) BETWEEN 1 AND 160', name='ck_team_workspace_name'),
    )


class Membership(TeamBase):
    __tablename__ = 'team_memberships'
    workspace_id: Mapped[str] = mapped_column(ForeignKey('team_workspaces.id'), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey('team_users.id'), primary_key=True)
    roles: Mapped[list] = mapped_column(JSONB)
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text('true'))
    version: Mapped[int] = mapped_column(Integer, default=1, server_default='1')
    __table_args__ = (
        CheckConstraint('version >= 1', name='ck_team_membership_version'),
        CheckConstraint(
            "jsonb_typeof(roles) = 'array' AND jsonb_array_length(roles) BETWEEN 1 AND 4 "
            "AND roles <@ '[\"workspace_manager\",\"operator\",\"reviewer\",\"viewer\"]'::jsonb",
            name='ck_team_membership_roles'),
        Index('ix_team_membership_user', 'user_id', 'active'),
    )


class Session(TeamBase):
    __tablename__ = 'team_sessions'
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey('team_users.id'))
    csrf_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    session_version: Mapped[int] = mapped_column(Integer)
    __table_args__ = (
        CheckConstraint('session_version >= 1', name='ck_team_session_version'),
        CheckConstraint('expires_at > created_at AND last_seen_at >= created_at', name='ck_team_session_dates'),
        Index('ix_team_session_user', 'user_id'),
        Index('ix_team_session_expiry', 'expires_at'),
    )


class AuditEvent(TeamBase):
    __tablename__ = 'team_audit_events'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    org_id: Mapped[str] = mapped_column(ForeignKey('team_organizations.id'))
    workspace_id: Mapped[str | None] = mapped_column(String(36))
    actor_id: Mapped[str | None] = mapped_column(ForeignKey('team_users.id'))
    action: Mapped[str] = mapped_column(String(80))
    resource_type: Mapped[str] = mapped_column(String(48))
    resource_id: Mapped[str | None] = mapped_column(String(128))
    request_id: Mapped[str] = mapped_column(String(64))
    safe_detail: Mapped[dict] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (
        ForeignKeyConstraint(['workspace_id', 'org_id'], ['team_workspaces.id', 'team_workspaces.org_id'], name='fk_team_audit_workspace_org'),
        CheckConstraint("jsonb_typeof(safe_detail) = 'object'", name='ck_team_audit_detail'),
        Index('ix_team_audit_workspace_time', 'workspace_id', 'created_at', 'id'),
        Index('ix_team_audit_org_time', 'org_id', 'created_at', 'id'),
    )


class LegacyScope(TeamBase):
    __tablename__ = 'team_legacy_scopes'
    resource_type: Mapped[str] = mapped_column(String(32), primary_key=True)
    legacy_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(ForeignKey('team_workspaces.id'))
    __table_args__ = (Index('ix_team_legacy_scope_workspace', 'workspace_id', 'resource_type'),)


class LoginThrottle(TeamBase):
    __tablename__ = 'team_login_throttles'
    key_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    failures: Mapped[int] = mapped_column(Integer, default=0, server_default='0')
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    blocked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint('failures >= 0', name='ck_team_login_failures'),
        Index('ix_team_login_window', 'window_start'),
    )


class MutationReceipt(TeamBase):
    __tablename__ = 'team_mutation_receipts'
    # The scope prefix distinguishes organization and workspace identifiers.
    scope_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor_id: Mapped[str] = mapped_column(ForeignKey('team_users.id'), primary_key=True)
    action: Mapped[str] = mapped_column(String(80), primary_key=True)
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    __table_args__ = (
        CheckConstraint('char_length(key) BETWEEN 1 AND 128', name='ck_team_receipt_key'),
        CheckConstraint("jsonb_typeof(result) = 'object'", name='ck_team_receipt_result'),
    )

"""Durable queue entities; migration 0004 owns their frozen DDL."""
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from api.db import new_id
from .models import TeamBase


class GlobalCapacity(TeamBase):
    __tablename__ = 'team_job_capacity'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pending_count: Mapped[int] = mapped_column(Integer, server_default='0')
    pending_bytes: Mapped[int] = mapped_column(BigInteger, server_default='0')
    active_count: Mapped[int] = mapped_column(Integer, server_default='0')
    max_pending: Mapped[int] = mapped_column(Integer, server_default='100')
    max_pending_bytes: Mapped[int] = mapped_column(BigInteger, server_default='1073741824')
    generation: Mapped[int] = mapped_column(BigInteger, server_default='1')
    recovery_hold: Mapped[bool] = mapped_column(Boolean, server_default=text('false'))
    __table_args__ = (CheckConstraint('id = 1 AND pending_count >= 0 AND pending_bytes >= 0 AND active_count BETWEEN 0 AND 1 AND max_pending > 0 AND max_pending_bytes > 0 AND generation > 0', name='ck_team_job_capacity'),)


class WorkspaceQuota(TeamBase):
    __tablename__ = 'team_job_workspace_quota'
    workspace_id: Mapped[str] = mapped_column(ForeignKey('team_workspaces.id'), primary_key=True)
    committed_bytes: Mapped[int] = mapped_column(BigInteger, server_default='0')
    reserved_bytes: Mapped[int] = mapped_column(BigInteger, server_default='0')
    limit_bytes: Mapped[int] = mapped_column(BigInteger, server_default='1073741824')
    pending_count: Mapped[int] = mapped_column(Integer, server_default='0')
    max_pending: Mapped[int] = mapped_column(Integer, server_default='20')
    __table_args__ = (CheckConstraint('committed_bytes >= 0 AND reserved_bytes >= 0 AND limit_bytes > 0 AND pending_count >= 0 AND max_pending > 0', name='ck_team_job_quota'),)


class Job(TeamBase):
    __tablename__ = 'team_jobs'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey('team_workspaces.id'))
    actor_id: Mapped[str] = mapped_column(ForeignKey('team_users.id'))
    kind: Mapped[str] = mapped_column(String(24))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    request_snapshot: Mapped[dict] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(String(24), server_default='queued')
    attempt: Mapped[int] = mapped_column(Integer, server_default='0')
    fence_token: Mapped[str | None] = mapped_column(String(36))
    worker_id: Mapped[str | None] = mapped_column(String(128))
    generation: Mapped[int | None] = mapped_column(BigInteger)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text('clock_timestamp()'))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reserved_bytes: Mapped[int] = mapped_column(BigInteger)
    base_bytes: Mapped[int] = mapped_column(BigInteger)
    retained_bytes: Mapped[int] = mapped_column(BigInteger, server_default='0')
    external_retained_bytes: Mapped[int] = mapped_column(BigInteger, server_default='0')
    result: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    __table_args__ = (
        UniqueConstraint('workspace_id', 'actor_id', 'kind', 'idempotency_key', name='uq_team_job_idempotency'),
        UniqueConstraint('workspace_id', 'id', name='uq_team_job_workspace'),
        CheckConstraint("kind IN ('import','reconcile','export')", name='ck_team_job_kind'),
        CheckConstraint("state IN ('queued','running','cancel_requested','succeeded','failed','cancelled','interrupted')", name='ck_team_job_state'),
        CheckConstraint("jsonb_typeof(request_snapshot) = 'object'", name='ck_team_job_request'),
        CheckConstraint('reserved_bytes >= 0 AND base_bytes > 0 AND retained_bytes >= 0 AND attempt >= 0', name='ck_team_job_bytes'),
        Index('uq_team_job_active', text('(1)'), unique=True, postgresql_where=text("state IN ('running','cancel_requested')")),
        Index('ix_team_job_queue', 'state', 'created_at', 'id'),
        Index('ix_team_job_workspace', 'workspace_id', 'created_at', 'id'),
    )


class JobAttempt(TeamBase):
    __tablename__ = 'team_job_attempts'
    job_id: Mapped[str] = mapped_column(ForeignKey('team_jobs.id'), primary_key=True)
    attempt: Mapped[int] = mapped_column(Integer, primary_key=True)
    fence_token: Mapped[str] = mapped_column(String(36), unique=True)
    worker_id: Mapped[str] = mapped_column(String(128))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(24))


class QuotaLedger(TeamBase):
    __tablename__ = 'team_job_quota_ledger'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(String(36))
    job_id: Mapped[str] = mapped_column(String(36))
    event: Mapped[str] = mapped_column(String(16))
    reserved_delta: Mapped[int] = mapped_column(BigInteger)
    committed_delta: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text('clock_timestamp()'))
    __table_args__ = (
        ForeignKeyConstraint(['workspace_id','job_id'], ['team_jobs.workspace_id','team_jobs.id']),
        UniqueConstraint('job_id', 'event', name='uq_team_job_ledger_event'),
        CheckConstraint("event IN ('reserve','settle')", name='ck_team_job_ledger_event'),
    )

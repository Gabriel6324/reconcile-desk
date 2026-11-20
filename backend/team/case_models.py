"""Mutable case disposition with immutable computation and append-only history."""
from datetime import datetime
from sqlalchemy import String,Integer,BigInteger,DateTime,ForeignKey,ForeignKeyConstraint,UniqueConstraint,CheckConstraint,Index,text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped,mapped_column
from .models import TeamBase
from . import period_models,reconcile_models


class CaseBatch(TeamBase):
    __tablename__='team_case_batches'
    run_id: Mapped[str]=mapped_column(String(36),primary_key=True)
    workspace_id: Mapped[str]=mapped_column(String(36))
    period_id: Mapped[str]=mapped_column(String(36))
    policy_version: Mapped[str]=mapped_column(String(64))
    total_count: Mapped[int]=mapped_column(Integer)
    accounted_bytes: Mapped[int]=mapped_column(BigInteger)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),server_default=text('clock_timestamp()'))
    __table_args__=(UniqueConstraint('workspace_id','period_id','run_id'),
        ForeignKeyConstraint(['workspace_id','period_id','run_id'],['team_period_runs.workspace_id','team_period_runs.period_id','team_period_runs.run_id']),
        CheckConstraint('total_count >= 0 AND accounted_bytes > 0'))


class DifferenceCase(TeamBase):
    __tablename__='team_difference_cases'
    id: Mapped[str]=mapped_column(String(36),primary_key=True)
    workspace_id: Mapped[str]=mapped_column(String(36))
    period_id: Mapped[str]=mapped_column(String(36))
    run_id: Mapped[str]=mapped_column(String(36))
    group_id: Mapped[str]=mapped_column(String(66))
    kind: Mapped[str]=mapped_column(String(32))
    state: Mapped[str]=mapped_column(String(24),server_default='open')
    version: Mapped[int]=mapped_column(Integer,server_default='1')
    assigned_to: Mapped[str|None]=mapped_column(ForeignKey('team_users.id'))
    submitted_by: Mapped[str|None]=mapped_column(ForeignKey('team_users.id'))
    reviewed_by: Mapped[str|None]=mapped_column(ForeignKey('team_users.id'))
    resolution_json: Mapped[dict|None]=mapped_column(JSONB(none_as_null=True))
    resolution_hash: Mapped[str|None]=mapped_column(String(64))
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),server_default=text('clock_timestamp()'))
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),server_default=text('clock_timestamp()'))
    __table_args__=(UniqueConstraint('workspace_id','id'),UniqueConstraint('workspace_id','run_id','group_id'),
        ForeignKeyConstraint(['workspace_id','period_id','run_id'],['team_case_batches.workspace_id','team_case_batches.period_id','team_case_batches.run_id']),
        ForeignKeyConstraint(['workspace_id','run_id','group_id'],['team_collection_groups.workspace_id','team_collection_groups.run_id','team_collection_groups.group_id']),
        CheckConstraint('version > 0'),CheckConstraint("state IN ('open','in_progress','pending_review','closed')"),
        CheckConstraint("kind <> 'exact_equal'"),
        CheckConstraint("state='open' OR assigned_to IS NOT NULL"),
        CheckConstraint("state NOT IN ('pending_review','closed') OR (submitted_by IS NOT NULL AND resolution_json IS NOT NULL AND resolution_hash IS NOT NULL)"),
        CheckConstraint("state<>'closed' OR (reviewed_by IS NOT NULL AND reviewed_by<>submitted_by)"),
        CheckConstraint('(resolution_json IS NULL)=(resolution_hash IS NULL)'),
        Index('ix_team_case_period_run','workspace_id','period_id','run_id','state','id'))


class CaseEvent(TeamBase):
    __tablename__='team_case_events'
    id: Mapped[str]=mapped_column(String(36),primary_key=True)
    workspace_id: Mapped[str]=mapped_column(String(36))
    case_id: Mapped[str]=mapped_column(String(36))
    sequence: Mapped[int]=mapped_column(Integer)
    action: Mapped[str]=mapped_column(String(32))
    actor_id: Mapped[str|None]=mapped_column(ForeignKey('team_users.id'))
    detail: Mapped[dict]=mapped_column(JSONB)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),server_default=text('clock_timestamp()'))
    __table_args__=(ForeignKeyConstraint(['workspace_id','case_id'],['team_difference_cases.workspace_id','team_difference_cases.id']),
        UniqueConstraint('case_id','sequence'),CheckConstraint('sequence > 0'))

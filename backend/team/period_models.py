"""Reusable collection profiles and immutable period configuration history."""
from datetime import datetime
from sqlalchemy import String,Integer,BigInteger,DateTime,ForeignKey,ForeignKeyConstraint,UniqueConstraint,CheckConstraint,text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped,mapped_column
from .models import TeamBase
from . import source_models,job_models,reconcile_models


class CollectionProfile(TeamBase):
    __tablename__='team_collection_profiles'
    id: Mapped[str]=mapped_column(String(36),primary_key=True)
    workspace_id: Mapped[str]=mapped_column(ForeignKey('team_workspaces.id'))
    source_rule_id: Mapped[str]=mapped_column(String(36))
    name: Mapped[str]=mapped_column(String(120))
    definition_json: Mapped[dict]=mapped_column(JSONB)
    profile_hash: Mapped[str]=mapped_column(String(64))
    created_by: Mapped[str]=mapped_column(ForeignKey('team_users.id'))
    accounted_bytes: Mapped[int]=mapped_column(BigInteger)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),server_default=text('clock_timestamp()'))
    __table_args__=(UniqueConstraint('workspace_id','id'),ForeignKeyConstraint(['workspace_id','source_rule_id'],['team_collection_rules.workspace_id','team_collection_rules.id']),CheckConstraint('accounted_bytes > 0'))


class BusinessPeriod(TeamBase):
    __tablename__='team_business_periods'
    id: Mapped[str]=mapped_column(String(36),primary_key=True)
    workspace_id: Mapped[str]=mapped_column(ForeignKey('team_workspaces.id'))
    definition_json: Mapped[dict]=mapped_column(JSONB)
    definition_hash: Mapped[str]=mapped_column(String(64))
    version: Mapped[int]=mapped_column(Integer,server_default='1')
    revision_number: Mapped[int]=mapped_column(Integer,server_default='0')
    current_revision_id: Mapped[str|None]=mapped_column(String(36))
    confirmed_revision_id: Mapped[str|None]=mapped_column(String(36))
    active_run_id: Mapped[str|None]=mapped_column(String(36))
    state: Mapped[str]=mapped_column(String(16),server_default='draft')
    created_by: Mapped[str]=mapped_column(ForeignKey('team_users.id'))
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),server_default=text('clock_timestamp()'))
    __table_args__=(UniqueConstraint('workspace_id','id'),
        ForeignKeyConstraint(['workspace_id','id','current_revision_id'],['team_period_revisions.workspace_id','team_period_revisions.period_id','team_period_revisions.id'],use_alter=True,name='fk_period_current_revision'),
        ForeignKeyConstraint(['workspace_id','id','confirmed_revision_id'],['team_period_revisions.workspace_id','team_period_revisions.period_id','team_period_revisions.id'],use_alter=True,name='fk_period_confirmed_revision'),
        ForeignKeyConstraint(['workspace_id','id','active_run_id'],['team_period_runs.workspace_id','team_period_runs.period_id','team_period_runs.run_id'],use_alter=True,name='fk_period_active_run'),
        CheckConstraint('version > 0 AND revision_number >= 0'),
        CheckConstraint("state IN ('draft','ready','reviewing','closed')"),
        CheckConstraint("state='draft' OR confirmed_revision_id IS NOT NULL",name='ck_period_confirmation_present'),
        CheckConstraint("state NOT IN ('reviewing','closed') OR active_run_id IS NOT NULL",name='ck_period_review_active'),
        CheckConstraint("(state='draft' AND confirmed_revision_id IS NULL) OR (state IN ('ready','reviewing','closed') AND current_revision_id IS NOT NULL AND confirmed_revision_id=current_revision_id)"))


class PeriodRevision(TeamBase):
    __tablename__='team_period_revisions'
    id: Mapped[str]=mapped_column(String(36),primary_key=True)
    workspace_id: Mapped[str]=mapped_column(String(36))
    period_id: Mapped[str]=mapped_column(String(36))
    number: Mapped[int]=mapped_column(Integer)
    profile_id: Mapped[str]=mapped_column(String(36))
    rule_id: Mapped[str]=mapped_column(String(36))
    config_json: Mapped[dict]=mapped_column(JSONB)
    config_hash: Mapped[str]=mapped_column(String(64))
    created_by: Mapped[str]=mapped_column(ForeignKey('team_users.id'))
    accounted_bytes: Mapped[int]=mapped_column(BigInteger)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),server_default=text('clock_timestamp()'))
    __table_args__=(UniqueConstraint('workspace_id','period_id','id'),UniqueConstraint('period_id','number'),
        ForeignKeyConstraint(['workspace_id','period_id'],['team_business_periods.workspace_id','team_business_periods.id']),
        ForeignKeyConstraint(['workspace_id','profile_id'],['team_collection_profiles.workspace_id','team_collection_profiles.id']),
        ForeignKeyConstraint(['workspace_id','rule_id'],['team_collection_rules.workspace_id','team_collection_rules.id']),CheckConstraint('number > 0 AND accounted_bytes > 0'))


class PeriodRun(TeamBase):
    __tablename__='team_period_runs'
    run_id: Mapped[str]=mapped_column(String(36),primary_key=True)
    workspace_id: Mapped[str]=mapped_column(String(36))
    period_id: Mapped[str]=mapped_column(String(36))
    revision_id: Mapped[str]=mapped_column(String(36))
    config_hash: Mapped[str]=mapped_column(String(64))
    submitted_by: Mapped[str]=mapped_column(ForeignKey('team_users.id'))
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),server_default=text('clock_timestamp()'))
    __table_args__=(UniqueConstraint('workspace_id','period_id','run_id'),
        ForeignKeyConstraint(['workspace_id','period_id','revision_id'],['team_period_revisions.workspace_id','team_period_revisions.period_id','team_period_revisions.id']),
        ForeignKeyConstraint(['workspace_id','run_id'],['team_collection_runs.workspace_id','team_collection_runs.id']))

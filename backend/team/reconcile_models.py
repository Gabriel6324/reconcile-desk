"""Confirmed rule versions, immutable run results and row-level provenance."""
from datetime import datetime
from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Integer, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from api.db import new_id
from .models import TeamBase


class CollectionRule(TeamBase):
    __tablename__ = 'team_collection_rules'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey('team_workspaces.id'))
    name: Mapped[str] = mapped_column(String(120))
    published_by: Mapped[str] = mapped_column(ForeignKey('team_users.id'))
    left_dataset_id: Mapped[str] = mapped_column(String(36))
    right_dataset_id: Mapped[str] = mapped_column(String(36))
    definition_json: Mapped[dict] = mapped_column(JSONB)
    rule_hash: Mapped[str] = mapped_column(String(64))
    accounted_bytes: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text('clock_timestamp()'))
    __table_args__ = (
        UniqueConstraint('workspace_id', 'id'),
        ForeignKeyConstraint(['workspace_id', 'left_dataset_id'], ['team_datasets.workspace_id', 'team_datasets.id']),
        ForeignKeyConstraint(['workspace_id', 'right_dataset_id'], ['team_datasets.workspace_id', 'team_datasets.id']),
        CheckConstraint('left_dataset_id <> right_dataset_id AND accounted_bytes > 0'),
    )


class CollectionRun(TeamBase):
    __tablename__ = 'team_collection_runs'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    workspace_id: Mapped[str] = mapped_column(ForeignKey('team_workspaces.id'))
    rule_id: Mapped[str] = mapped_column(String(36))
    job_id: Mapped[str | None] = mapped_column(String(36), unique=True)
    input_hash: Mapped[str] = mapped_column(String(64))
    result_hash: Mapped[str | None] = mapped_column(String(64))
    summary_json: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=text('clock_timestamp()'))
    __table_args__ = (
        UniqueConstraint('workspace_id', 'id'),
        ForeignKeyConstraint(['workspace_id', 'rule_id'], ['team_collection_rules.workspace_id', 'team_collection_rules.id']),
        ForeignKeyConstraint(['workspace_id', 'job_id'], ['team_jobs.workspace_id', 'team_jobs.id']),
    )


class ResultGroup(TeamBase):
    __tablename__ = 'team_collection_groups'
    workspace_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    group_id: Mapped[str] = mapped_column(String(66), primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    detail: Mapped[dict] = mapped_column(JSONB)
    __table_args__ = (
        ForeignKeyConstraint(['workspace_id', 'run_id'], ['team_collection_runs.workspace_id', 'team_collection_runs.id']),
        UniqueConstraint('run_id', 'ordinal'),
        CheckConstraint('ordinal > 0'),
    )


class RowCoverage(TeamBase):
    __tablename__ = 'team_collection_coverage'
    workspace_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    side: Mapped[str] = mapped_column(String(8), primary_key=True)
    dataset_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    row_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    group_id: Mapped[str] = mapped_column(String(66))
    disposition: Mapped[str] = mapped_column(String(32))
    __table_args__ = (
        ForeignKeyConstraint(['workspace_id', 'run_id', 'group_id'], ['team_collection_groups.workspace_id', 'team_collection_groups.run_id', 'team_collection_groups.group_id']),
        ForeignKeyConstraint(['workspace_id', 'dataset_id', 'row_id'], ['team_dataset_rows.workspace_id', 'team_dataset_rows.dataset_id', 'team_dataset_rows.row_id']),
        CheckConstraint("side IN ('left','right')"),
    )

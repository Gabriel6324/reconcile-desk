"""Immutable upload assets and versioned parsed rows; migration 0005 is frozen."""
from datetime import datetime
from sqlalchemy import String, Integer, BigInteger, DateTime, LargeBinary, ForeignKey, ForeignKeyConstraint, UniqueConstraint, CheckConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from .models import TeamBase
from api.db import new_id


class SourceAsset(TeamBase):
    __tablename__='team_source_assets'
    id: Mapped[str]=mapped_column(String(36),primary_key=True,default=new_id)
    workspace_id: Mapped[str]=mapped_column(ForeignKey('team_workspaces.id'))
    created_by: Mapped[str]=mapped_column(ForeignKey('team_users.id'))
    filename: Mapped[str]=mapped_column(String(240))
    sha256: Mapped[str]=mapped_column(String(64))
    content: Mapped[bytes]=mapped_column(LargeBinary)
    accounted_bytes: Mapped[int]=mapped_column(BigInteger)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),server_default=text('clock_timestamp()'))
    __table_args__=(UniqueConstraint('workspace_id','id'),CheckConstraint('accounted_bytes >= octet_length(content)'))


class Dataset(TeamBase):
    __tablename__='team_datasets'
    id: Mapped[str]=mapped_column(String(36),primary_key=True,default=new_id)
    workspace_id: Mapped[str]=mapped_column(ForeignKey('team_workspaces.id'))
    asset_id: Mapped[str]=mapped_column(String(36))
    job_id: Mapped[str|None]=mapped_column(String(36),unique=True)
    worksheet: Mapped[str|None]=mapped_column(String(240))
    metadata_json: Mapped[dict|None]=mapped_column(JSONB(none_as_null=True))
    parsed_hash: Mapped[str|None]=mapped_column(String(64))
    error_details: Mapped[dict|None]=mapped_column(JSONB(none_as_null=True))
    row_count: Mapped[int]=mapped_column(Integer,server_default='0')
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),server_default=text('clock_timestamp()'))
    __table_args__=(UniqueConstraint('workspace_id','id'),
        ForeignKeyConstraint(['workspace_id','asset_id'],['team_source_assets.workspace_id','team_source_assets.id']),
        ForeignKeyConstraint(['workspace_id','job_id'],['team_jobs.workspace_id','team_jobs.id']),CheckConstraint('row_count >= 0'))


class DatasetRow(TeamBase):
    __tablename__='team_dataset_rows'
    workspace_id: Mapped[str]=mapped_column(String(36),primary_key=True)
    dataset_id: Mapped[str]=mapped_column(String(36),primary_key=True)
    row_id: Mapped[str]=mapped_column(String(32),primary_key=True)
    ordinal: Mapped[int]=mapped_column(Integer)
    source: Mapped[dict]=mapped_column(JSONB)
    cells: Mapped[dict]=mapped_column(JSONB)
    __table_args__=(ForeignKeyConstraint(['workspace_id','dataset_id'],['team_datasets.workspace_id','team_datasets.id']),
        UniqueConstraint('dataset_id','ordinal'),CheckConstraint('ordinal > 0'))

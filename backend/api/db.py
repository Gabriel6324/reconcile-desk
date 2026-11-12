"""PostgreSQL persistence. No SQLite fallback; all writes are fenced by executor epoch."""
import os
import uuid
from datetime import datetime, timezone
from sqlalchemy import (create_engine, String, Integer, BigInteger, DateTime, ForeignKey,
                        LargeBinary, UniqueConstraint, Index, CheckConstraint, text)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

def now():
    return datetime.now(timezone.utc)

def new_id():
    return str(uuid.uuid4())

class Base(DeclarativeBase):
    pass

class Executor(Base):
    __tablename__ = 'executor_state'
    id: Mapped[int] = mapped_column(primary_key=True)
    epoch: Mapped[int] = mapped_column(BigInteger, default=0)
    instance: Mapped[str] = mapped_column(String(36), default='')

class Quota(Base):
    __tablename__ = 'workspace_quota'
    id: Mapped[int] = mapped_column(primary_key=True)
    committed: Mapped[int] = mapped_column(BigInteger, default=0)
    reserved: Mapped[int] = mapped_column(BigInteger, default=0)
    limit_bytes: Mapped[int] = mapped_column(BigInteger, default=1073741824)
    __table_args__ = (CheckConstraint('committed >= 0 AND reserved >= 0'),)

class Operation(Base):
    __tablename__ = 'operations'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(32))
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    token: Mapped[str] = mapped_column(String(36), default=new_id)
    epoch: Mapped[int] = mapped_column(BigInteger)
    state: Mapped[str] = mapped_column(String(16), default='running')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stop_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resource_id: Mapped[str | None] = mapped_column(String(36))
    resource_kind: Mapped[str | None] = mapped_column(String(32))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(String(500))
    retry_of: Mapped[str | None] = mapped_column(ForeignKey('operations.id'))
    request_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)
    reserved: Mapped[int] = mapped_column(BigInteger, default=0)
    retained: Mapped[int] = mapped_column(BigInteger, default=0)
    __table_args__ = (
        CheckConstraint("state IN ('running','stopping','completed','failed','interrupted')"),
        Index('uq_active_operation', text('(1)'), unique=True,
              postgresql_where=text("state IN ('running','stopping')")),
    )

class Import(Base):
    __tablename__ = 'imports'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    operation_id: Mapped[str] = mapped_column(ForeignKey('operations.id'), unique=True)
    filename: Mapped[str] = mapped_column(String(240))
    sha256: Mapped[str] = mapped_column(String(64))
    original: Mapped[bytes] = mapped_column(LargeBinary)
    parsed: Mapped[dict | None] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(String(16), default='processing')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

class ImportRow(Base):
    __tablename__ = 'import_rows'
    import_id: Mapped[str] = mapped_column(ForeignKey('imports.id'), primary_key=True)
    row_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    __table_args__ = (UniqueConstraint('import_id', 'ordinal'),)

class Mapping(Base):
    __tablename__ = 'mappings'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    left_id: Mapped[str] = mapped_column(ForeignKey('imports.id'))
    right_id: Mapped[str] = mapped_column(ForeignKey('imports.id'))
    current_revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

class Revision(Base):
    __tablename__ = 'mapping_revisions'
    mapping_id: Mapped[str] = mapped_column(ForeignKey('mappings.id'), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    definition: Mapped[dict] = mapped_column(JSONB)
    origin: Mapped[str] = mapped_column(String(32), default='manual')
    confirmation: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

class Run(Base):
    __tablename__ = 'runs'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    operation_id: Mapped[str] = mapped_column(ForeignKey('operations.id'), unique=True)
    mapping_id: Mapped[str] = mapped_column(ForeignKey('mappings.id'))
    revision: Mapped[int] = mapped_column(Integer)
    left_id: Mapped[str] = mapped_column(ForeignKey('imports.id'))
    right_id: Mapped[str] = mapped_column(ForeignKey('imports.id'))
    snapshot: Mapped[dict] = mapped_column(JSONB)
    result: Mapped[dict | None] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(String(16), default='processing')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

class Coverage(Base):
    __tablename__ = 'result_group_rows'
    run_id: Mapped[str] = mapped_column(ForeignKey('runs.id'), primary_key=True)
    side: Mapped[str] = mapped_column(String(5), primary_key=True)
    row_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    import_id: Mapped[str] = mapped_column(ForeignKey('imports.id'))
    group_id: Mapped[str] = mapped_column(String(64))
    __table_args__ = (CheckConstraint("side IN ('left','right')"),)

class Export(Base):
    __tablename__ = 'exports'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey('runs.id'))
    operation_id: Mapped[str] = mapped_column(ForeignKey('operations.id'), unique=True)
    version: Mapped[str] = mapped_column(String(16), default='1')
    content: Mapped[bytes] = mapped_column(LargeBinary)
    sha256: Mapped[str] = mapped_column(String(64))
    __table_args__ = (UniqueConstraint('run_id', 'version'),)

class Suggestion(Base):
    __tablename__ = 'model_calls'
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    mapping_id: Mapped[str] = mapped_column(ForeignKey('mappings.id'))
    operation_id: Mapped[str] = mapped_column(ForeignKey('operations.id'), unique=True)
    base_revision: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(32), default='running')
    billing_state: Mapped[str] = mapped_column(String(32), default='not_sent')
    metadata_json: Mapped[dict] = mapped_column(JSONB, default=dict)
    proposal: Mapped[dict | None] = mapped_column(JSONB)

def make_engine():
    url = os.environ.get('DATABASE_URL', '')
    if not url.startswith(('postgresql://', 'postgresql+psycopg://')):
        raise RuntimeError('DATABASE_URL must point to PostgreSQL')
    if url.startswith('postgresql://'):
        url = url.replace('postgresql://', 'postgresql+psycopg://', 1)
    return create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=3,
                         connect_args={'connect_timeout': 5})

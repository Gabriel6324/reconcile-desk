"""Frozen schema emitted by the released v0.2 bootstrap migration.

This module deliberately has no dependency on application ORM models. Python
column defaults in the original ORM were not database defaults and are omitted.
Keep this historical DDL stable; all future changes belong to later revisions.

Source: released v0.2 backend/api/db.py, SHA-256
b214f78a481764b458995e288bd256870971cc530471d7da7470898eb42f4981.
The v0.2 fresh-install path already created mapping_templates. Revision 0002
uses checkfirst to also support the earlier v0.1 / 0001 schema without it.
"""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


def metadata():
    """Return a new, standalone MetaData for exactly the historical tables."""
    schema = sa.MetaData()
    sa.Table('executor_state', schema,
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('epoch', sa.BigInteger(), nullable=False),
        sa.Column('instance', sa.String(36), nullable=False))
    sa.Table('workspace_quota', schema,
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('committed', sa.BigInteger(), nullable=False),
        sa.Column('reserved', sa.BigInteger(), nullable=False),
        sa.Column('limit_bytes', sa.BigInteger(), nullable=False),
        sa.CheckConstraint('committed >= 0 AND reserved >= 0'))
    sa.Table('operations', schema,
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('kind', sa.String(32), nullable=False),
        sa.Column('idempotency_key', sa.String(128), nullable=False, unique=True),
        sa.Column('request_hash', sa.String(64), nullable=False),
        sa.Column('token', sa.String(36), nullable=False),
        sa.Column('epoch', sa.BigInteger(), nullable=False),
        sa.Column('state', sa.String(16), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('finished_at', sa.DateTime(timezone=True)),
        sa.Column('stop_requested_at', sa.DateTime(timezone=True)),
        sa.Column('resource_id', sa.String(36)),
        sa.Column('resource_kind', sa.String(32)),
        sa.Column('error_code', sa.String(64)),
        sa.Column('error_message', sa.String(500)),
        sa.Column('retry_of', sa.String(36), sa.ForeignKey('operations.id')),
        sa.Column('request_snapshot', JSONB(), nullable=False),
        sa.Column('reserved', sa.BigInteger(), nullable=False),
        sa.Column('retained', sa.BigInteger(), nullable=False),
        sa.CheckConstraint("state IN ('running','stopping','completed','failed','interrupted')"),
        sa.Index('uq_active_operation', sa.text('(1)'), unique=True,
                 postgresql_where=sa.text("state IN ('running','stopping')")))
    sa.Table('imports', schema,
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('operation_id', sa.String(36), sa.ForeignKey('operations.id'), nullable=False, unique=True),
        sa.Column('filename', sa.String(240), nullable=False),
        sa.Column('sha256', sa.String(64), nullable=False),
        sa.Column('original', sa.LargeBinary(), nullable=False),
        sa.Column('parsed', JSONB()),
        sa.Column('state', sa.String(16), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False))
    sa.Table('import_rows', schema,
        sa.Column('import_id', sa.String(36), sa.ForeignKey('imports.id'), primary_key=True),
        sa.Column('row_id', sa.String(32), primary_key=True),
        sa.Column('ordinal', sa.Integer(), nullable=False),
        sa.UniqueConstraint('import_id', 'ordinal'))
    sa.Table('mappings', schema,
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('left_id', sa.String(36), sa.ForeignKey('imports.id'), nullable=False),
        sa.Column('right_id', sa.String(36), sa.ForeignKey('imports.id'), nullable=False),
        sa.Column('current_revision', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False))
    sa.Table('mapping_revisions', schema,
        sa.Column('mapping_id', sa.String(36), sa.ForeignKey('mappings.id'), primary_key=True),
        sa.Column('revision', sa.Integer(), primary_key=True),
        sa.Column('definition', JSONB(), nullable=False),
        sa.Column('origin', sa.String(32), nullable=False),
        sa.Column('confirmation', JSONB()),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False))
    sa.Table('mapping_templates', schema,
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('name', sa.String(80), nullable=False, unique=True),
        sa.Column('specification', JSONB(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint('char_length(name) BETWEEN 1 AND 80'))
    sa.Table('runs', schema,
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('operation_id', sa.String(36), sa.ForeignKey('operations.id'), nullable=False, unique=True),
        sa.Column('mapping_id', sa.String(36), sa.ForeignKey('mappings.id'), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('left_id', sa.String(36), sa.ForeignKey('imports.id'), nullable=False),
        sa.Column('right_id', sa.String(36), sa.ForeignKey('imports.id'), nullable=False),
        sa.Column('snapshot', JSONB(), nullable=False),
        sa.Column('result', JSONB()),
        sa.Column('state', sa.String(16), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False))
    sa.Table('result_group_rows', schema,
        sa.Column('run_id', sa.String(36), sa.ForeignKey('runs.id'), primary_key=True),
        sa.Column('side', sa.String(5), primary_key=True),
        sa.Column('row_id', sa.String(32), primary_key=True),
        sa.Column('import_id', sa.String(36), sa.ForeignKey('imports.id'), nullable=False),
        sa.Column('group_id', sa.String(64), nullable=False),
        sa.CheckConstraint("side IN ('left','right')"))
    sa.Table('exports', schema,
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('run_id', sa.String(36), sa.ForeignKey('runs.id'), nullable=False),
        sa.Column('operation_id', sa.String(36), sa.ForeignKey('operations.id'), nullable=False, unique=True),
        sa.Column('version', sa.String(16), nullable=False),
        sa.Column('content', sa.LargeBinary(), nullable=False),
        sa.Column('sha256', sa.String(64), nullable=False),
        sa.UniqueConstraint('run_id', 'version'))
    sa.Table('model_calls', schema,
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('mapping_id', sa.String(36), sa.ForeignKey('mappings.id'), nullable=False),
        sa.Column('operation_id', sa.String(36), sa.ForeignKey('operations.id'), nullable=False, unique=True),
        sa.Column('base_revision', sa.Integer(), nullable=False),
        sa.Column('state', sa.String(32), nullable=False),
        sa.Column('billing_state', sa.String(32), nullable=False),
        sa.Column('metadata_json', JSONB(), nullable=False),
        sa.Column('proposal', JSONB()))
    return schema

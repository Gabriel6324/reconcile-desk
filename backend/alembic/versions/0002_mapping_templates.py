"""Add reusable mapping templates without changing existing records."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade():
    # 0001 bootstraps fresh databases from ORM metadata. Its fresh-install path
    # already includes this table; legacy 0001 databases still need it created.
    table = sa.Table('mapping_templates', sa.MetaData(),
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('name', sa.String(80), nullable=False, unique=True),
        sa.Column('specification', JSONB(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint('char_length(name) BETWEEN 1 AND 80'))
    table.create(op.get_bind(), checkfirst=True)


def downgrade():
    raise RuntimeError('Destructive downgrade is intentionally unsupported; restore a backup instead.')

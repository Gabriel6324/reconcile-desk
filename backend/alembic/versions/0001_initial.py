"""Initial bounded workspace schema, frozen to the released v0.2 bootstrap."""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from alembic import op

# Load by file path: the installed Alembic package owns the ``alembic`` namespace.
# Never import live application models from a historical migration.
_path = Path(__file__).resolve().parents[1] / 'legacy_v02_schema.py'
_spec = spec_from_file_location('_reconcile_legacy_v02_schema', _path)
_legacy = module_from_spec(_spec)
_spec.loader.exec_module(_legacy)
revision = '0001'
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
    _legacy.metadata().create_all(op.get_bind())

def downgrade():
    raise RuntimeError('Destructive downgrade is intentionally unsupported; restore a backup instead.')

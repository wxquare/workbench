"""Bind synthetic result candidate and projected hashes atomically."""
from alembic import op
from workbench_agent.persistence.schema_v14 import synthetic_result_bindings
revision='0015_synthetic_result_bindings'
down_revision='0014_synthetic_alert_sources'
branch_labels=None
depends_on=None

def upgrade(): synthetic_result_bindings.create(op.get_bind(),checkfirst=False)
def downgrade(): raise RuntimeError('synthetic result binding downgrade requires backup restoration')

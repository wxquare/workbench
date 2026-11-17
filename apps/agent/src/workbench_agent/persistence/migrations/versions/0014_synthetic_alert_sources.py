"""Persist signed synthetic source metadata with Run admission."""
from alembic import op
from workbench_agent.persistence.schema_v13 import synthetic_alert_sources
revision='0014_synthetic_alert_sources'
down_revision='0013_action_policy'
branch_labels=None
depends_on=None

def upgrade(): synthetic_alert_sources.create(op.get_bind(),checkfirst=False)
def downgrade(): raise RuntimeError('synthetic alert source downgrade requires backup restoration')

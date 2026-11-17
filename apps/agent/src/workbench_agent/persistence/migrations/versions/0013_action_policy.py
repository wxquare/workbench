"""Create an authoritative deployment policy for external actions."""
from alembic import op
from workbench_agent.persistence.schema_v12 import action_policy_state
revision='0013_action_policy'
down_revision='0012_schedule_manual_fires'
branch_labels=None
depends_on=None

def upgrade(): action_policy_state.create(op.get_bind(),checkfirst=False)
def downgrade(): raise RuntimeError('action policy downgrade requires backup restoration')

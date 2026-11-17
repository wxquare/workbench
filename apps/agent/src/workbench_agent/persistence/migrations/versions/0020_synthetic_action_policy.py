"""Pin proposal and source policy revisions to synthetic actions."""
from alembic import op
from workbench_agent.persistence.schema_v19 import synthetic_action_policy_bindings
revision='0020_synthetic_action_policy'
down_revision='0019_synthetic_proposal_policy'
branch_labels=None
depends_on=None

def upgrade(): synthetic_action_policy_bindings.create(op.get_bind(),checkfirst=False)
def downgrade(): raise RuntimeError('synthetic action policy downgrade requires backup restoration')

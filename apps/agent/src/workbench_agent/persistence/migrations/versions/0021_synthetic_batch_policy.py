"""Create initially disabled bounded synthetic batch policy."""
from alembic import op
from workbench_agent.persistence.schema_v20 import synthetic_batch_policy
revision='0021_synthetic_batch_policy'
down_revision='0020_synthetic_action_policy'
branch_labels=None
depends_on=None

def upgrade(): synthetic_batch_policy.create(op.get_bind(),checkfirst=False)
def downgrade(): raise RuntimeError('synthetic batch policy downgrade requires backup restoration')

"""Create an initially disabled synthetic proposal policy."""
from alembic import op
from workbench_agent.persistence.schema_v18 import synthetic_proposal_policy
revision='0019_synthetic_proposal_policy'
down_revision='0018_incident_action_links'
branch_labels=None
depends_on=None

def upgrade(): synthetic_proposal_policy.create(op.get_bind(),checkfirst=False)
def downgrade(): raise RuntimeError('synthetic proposal policy downgrade requires backup restoration')

"""Install uninitialized synthetic source authority."""
from alembic import op
from workbench_agent.persistence.schema_v16 import synthetic_source_policy
revision='0017_synthetic_source_policy'
down_revision='0016_synthetic_identity'
branch_labels=None
depends_on=None

def upgrade(): synthetic_source_policy.create(op.get_bind(),checkfirst=False)
def downgrade(): raise RuntimeError('synthetic policy downgrade requires backup restoration')

"""Record action provenance for verified incident delivery links."""
from alembic import op
from workbench_agent.persistence.schema_v17 import incident_action_links
revision='0018_incident_action_links'
down_revision='0017_synthetic_source_policy'
branch_labels=None
depends_on=None

def upgrade(): incident_action_links.create(op.get_bind(),checkfirst=False)
def downgrade(): raise RuntimeError('incident action link downgrade requires backup restoration')

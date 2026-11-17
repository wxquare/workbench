"""Persist distinct manual inspection fire history."""
from alembic import op
from workbench_agent.persistence.schema_v11 import schedule_manual_fires
revision='0012_schedule_manual_fires'
down_revision='0011_workflow_versions'
branch_labels=None
depends_on=None

def upgrade(): schedule_manual_fires.create(op.get_bind(),checkfirst=False)
def downgrade(): raise RuntimeError('schedule history downgrade requires backup restoration')

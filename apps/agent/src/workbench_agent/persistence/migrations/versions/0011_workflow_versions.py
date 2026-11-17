"""Persist immutable workflow versions."""
from alembic import op
from workbench_agent.persistence.schema_v10 import workflow_versions
revision = '0011_workflow_versions'
down_revision = '0010_runner_cleanup'
branch_labels = None
depends_on = None

def upgrade():
    workflow_versions.create(op.get_bind(), checkfirst=False)

def downgrade():
    raise RuntimeError('workflow downgrade requires backup restoration')

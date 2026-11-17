"""Persist orphan Runner cleanup obligations."""
from alembic import op
from workbench_agent.persistence.schema_v9 import runner_cleanup_obligations
revision='0010_runner_cleanup'
down_revision='0009_artifact_upload_scopes'
branch_labels=None
depends_on=None

def upgrade(): runner_cleanup_obligations.create(op.get_bind(),checkfirst=False)
def downgrade(): raise RuntimeError('runner cleanup downgrade requires backup restoration')

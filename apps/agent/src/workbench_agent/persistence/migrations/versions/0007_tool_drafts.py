"""Add isolated mutable tool drafts; published bindings remain immutable."""
from alembic import op
from workbench_agent.persistence.schema_v6 import tool_binding_drafts
revision='0007_tool_drafts'
down_revision='0006_usage_ledger'
branch_labels=None
depends_on=None

def upgrade(): tool_binding_drafts.create(op.get_bind(),checkfirst=False)
def downgrade(): raise RuntimeError('tool draft downgrade requires backup restoration')

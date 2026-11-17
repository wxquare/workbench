"""Add redacted evidence content without fabricating historical payloads."""
from alembic import op
from workbench_agent.persistence.schema_v7 import evidence_payloads
revision='0008_evidence_payloads'
down_revision='0007_tool_drafts'
branch_labels=None
depends_on=None

def upgrade(): evidence_payloads.create(op.get_bind(),checkfirst=False)
def downgrade(): raise RuntimeError('evidence downgrade requires backup restoration')

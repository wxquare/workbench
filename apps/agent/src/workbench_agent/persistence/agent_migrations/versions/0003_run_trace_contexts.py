"""Persist only the W3C parent required to associate asynchronous execution."""
from alembic import op
from workbench_agent.persistence.schema_agent_v3 import run_trace_contexts

revision = '0003_run_trace_contexts'
down_revision = '0002_shared_skills'
branch_labels = None
depends_on = None


def upgrade():
    run_trace_contexts.create(op.get_bind(), checkfirst=False)


def downgrade():
    raise RuntimeError('restore a reviewed backup to downgrade')

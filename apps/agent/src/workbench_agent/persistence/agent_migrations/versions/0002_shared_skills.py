"""Add shared Skill drafts while preserving existing Agent storage."""
from alembic import op
from workbench_agent.persistence.schema_agent_v2 import skill_drafts

revision = '0002_shared_skills'
down_revision = '0001_agent_domain'
branch_labels = None
depends_on = None


def upgrade():
    skill_drafts.create(op.get_bind(), checkfirst=False)


def downgrade():
    raise RuntimeError('restore a reviewed backup to downgrade')

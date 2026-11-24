"""Add indexes used by authorized review candidates."""
from alembic import op

revision = '0007_review_indexes'
down_revision = '0006_multifile_skill_drafts'
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        'ix_review_agents_team_status',
        'agents',
        ['team_id', 'status', 'agent_id'],
        unique=False,
    )
    op.create_index(
        'ix_review_runs_team_agent_status_waiting',
        'runs',
        ['team_id', 'agent_id', 'status', 'waiting_deadline_at', 'created_at'],
        unique=False,
    )
    op.create_index(
        'ix_review_interrupts_team_status_actor',
        'interrupts',
        ['team_id', 'status', 'created_at', 'interrupt_id'],
        unique=False,
    )
    op.create_index(
        'ix_review_actions_team_state_created',
        'actions',
        ['team_id', 'state', 'created_at', 'action_id'],
        unique=False,
    )
    op.create_index(
        'ix_review_approvals_team_subject_created',
        'approvals',
        ['team_id', 'subject', 'created_at', 'approval_id'],
        unique=False,
    )


def downgrade():
    raise RuntimeError('restore a reviewed backup to downgrade')

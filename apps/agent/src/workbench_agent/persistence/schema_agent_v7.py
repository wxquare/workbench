"""Agent schema with indexes for the authorized review inbox."""

from sqlalchemy import Index, MetaData

from workbench_agent.persistence.schema_agent_v6 import metadata as baseline

MIGRATION_HEAD = '0007_review_indexes'
metadata = MetaData(naming_convention=dict(baseline.naming_convention))
for table in baseline.tables.values():
    table.to_metadata(metadata)

for name, table_name, columns in (
    ('ix_review_agents_team_status', 'agents', ('team_id', 'status', 'agent_id')),
    (
        'ix_review_runs_team_agent_status_waiting',
        'runs',
        ('team_id', 'agent_id', 'status', 'waiting_deadline_at', 'created_at'),
    ),
    (
        'ix_review_interrupts_team_status_actor',
        'interrupts',
        ('team_id', 'status', 'created_at', 'interrupt_id'),
    ),
    (
        'ix_review_actions_team_state_created',
        'actions',
        ('team_id', 'state', 'created_at', 'action_id'),
    ),
    (
        'ix_review_approvals_team_subject_created',
        'approvals',
        ('team_id', 'subject', 'created_at', 'approval_id'),
    ),
):
    Index(name, *(metadata.tables[table_name].c[column] for column in columns))

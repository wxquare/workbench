"""Pin synthetic proposal and source authority to one action."""

from sqlalchemy import (
    MetaData,
    Table,
    Column,
    String,
    BigInteger,
    ForeignKeyConstraint,
    UniqueConstraint,
)
from workbench_agent.persistence.schema_v18 import metadata as previous

metadata = MetaData(naming_convention=dict(previous.naming_convention))
for table in previous.tables.values():
    table.to_metadata(metadata)
synthetic_action_policy_bindings = Table(
    'synthetic_action_policy_bindings',
    metadata,
    Column('team_id', String(36), nullable=False),
    Column('action_id', String(36), primary_key=True),
    Column('source_revision', BigInteger, nullable=False),
    Column('source_hash', String(64), nullable=False),
    Column('proposal_revision', BigInteger, nullable=False),
    Column('proposal_hash', String(64), nullable=False),
    UniqueConstraint('team_id', 'action_id'),
    ForeignKeyConstraint(
        ['team_id', 'action_id'], ['actions.team_id', 'actions.action_id'], ondelete='RESTRICT'
    ),
)

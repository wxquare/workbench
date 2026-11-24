"""Database-authoritative deployment policy for action dispatch."""

from sqlalchemy import MetaData, Table, Column, String, BigInteger, Text, CheckConstraint
from workbench_agent.persistence.schema_v11 import metadata as previous

metadata = MetaData(naming_convention=dict(previous.naming_convention))
for table in previous.tables.values():
    table.to_metadata(metadata)
action_policy_state = Table(
    'action_policy_state',
    metadata,
    Column('policy_id', String(16), primary_key=True),
    Column('revision', BigInteger, nullable=False),
    Column('policy_hash', String(64), nullable=False),
    Column('policy_json', Text, nullable=False),
    Column('updated_at', BigInteger, nullable=False),
    CheckConstraint("policy_id = 'deployment'", name='action_policy_singleton'),
)

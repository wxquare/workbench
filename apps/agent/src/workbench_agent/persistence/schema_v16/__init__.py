"""Database authority for synthetic source grants."""

from sqlalchemy import MetaData, Table, Column, String, Text, BigInteger, CheckConstraint
from workbench_agent.persistence.schema_v15 import metadata as previous

metadata = MetaData(naming_convention=dict(previous.naming_convention))
for table in previous.tables.values():
    table.to_metadata(metadata)
synthetic_source_policy = Table(
    'synthetic_source_policy',
    metadata,
    Column('policy_id', String(16), primary_key=True),
    Column('revision', BigInteger, nullable=False),
    Column('policy_hash', String(64), nullable=False),
    Column('policy_json', Text, nullable=False),
    Column('updated_at', BigInteger, nullable=False),
    CheckConstraint("policy_id = 'deployment'", name='synthetic_source_singleton'),
)

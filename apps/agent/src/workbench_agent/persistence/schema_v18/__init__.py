"""Explicit database authority for synthetic action proposal rules."""
from sqlalchemy import MetaData,Table,Column,String,Text,BigInteger,CheckConstraint
from workbench_agent.persistence.schema_v17 import metadata as previous
metadata=MetaData(naming_convention=dict(previous.naming_convention))
for table in previous.tables.values(): table.to_metadata(metadata)
synthetic_proposal_policy=Table('synthetic_proposal_policy',metadata,
    Column('policy_id',String(16),primary_key=True),Column('revision',BigInteger,nullable=False),
    Column('policy_hash',String(64),nullable=False),Column('policy_json',Text,nullable=False),
    Column('updated_at',BigInteger,nullable=False),CheckConstraint("policy_id = 'deployment'",name='synthetic_proposal_singleton'))

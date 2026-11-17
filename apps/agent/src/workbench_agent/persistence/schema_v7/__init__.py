"""Durable bounded source content for evidence (migration 0008)."""
from sqlalchemy import MetaData,Table,Column,String,Text,BigInteger,ForeignKeyConstraint,UniqueConstraint
from workbench_agent.persistence.schema_v6 import metadata as previous
metadata=MetaData(naming_convention=dict(previous.naming_convention))
for table in previous.tables.values(): table.to_metadata(metadata)
evidence_payloads=Table('evidence_payloads',metadata,
    Column('team_id',String(36),nullable=False),Column('evidence_id',String(36),primary_key=True),
    Column('run_id',String(36),nullable=False),Column('payload',Text,nullable=False),
    Column('truncated',BigInteger,nullable=False),Column('created_at',BigInteger,nullable=False),
    UniqueConstraint('team_id','evidence_id'),
    ForeignKeyConstraint(['team_id','evidence_id'],['evidence.team_id','evidence.evidence_id'],ondelete='RESTRICT'),
    ForeignKeyConstraint(['team_id','run_id'],['runs.team_id','runs.run_id'],ondelete='RESTRICT'))

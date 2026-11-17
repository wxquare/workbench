"""Immutable additive Outcome and public event envelope snapshot."""
from sqlalchemy import MetaData,Table,Column,String,Text,BigInteger,ForeignKeyConstraint,UniqueConstraint
from workbench_agent.persistence.schema_v3 import metadata as previous
metadata=MetaData(naming_convention=dict(previous.naming_convention))
for table in previous.tables.values(): table.to_metadata(metadata)
run_outcomes=Table('run_outcomes',metadata,
    Column('team_id',String(36),nullable=False),Column('run_id',String(36),primary_key=True),
    Column('result_id',String(36),nullable=False),Column('digest',String(64),nullable=False),
    Column('payload',Text,nullable=False),Column('message_id',String(36),nullable=False),
    Column('response',Text),Column('created_at',BigInteger,nullable=False),
    UniqueConstraint('team_id','result_id'),
    ForeignKeyConstraint(['team_id','run_id'],['runs.team_id','runs.run_id'],ondelete='RESTRICT'),
    ForeignKeyConstraint(['team_id','message_id'],['messages.team_id','messages.message_id'],ondelete='RESTRICT'))
for column in (Column('schema_version',String(16),nullable=False,server_default='1'),Column('message_id',String(36))):
    metadata.tables['run_events'].append_column(column)
# Durable high-water mark remains after physical event retention cleanup.
metadata.tables['runs'].append_column(Column('event_floor',BigInteger,nullable=False,server_default='0'))

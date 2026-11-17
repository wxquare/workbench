"""Durable Runner cleanup obligations, independent of Run ownership."""
from sqlalchemy import MetaData, Table, Column, String, BigInteger, ForeignKeyConstraint, UniqueConstraint
from workbench_agent.persistence.schema_v8 import metadata as previous
metadata=MetaData(naming_convention=dict(previous.naming_convention))
for table in previous.tables.values(): table.to_metadata(metadata)
runner_cleanup_obligations=Table('runner_cleanup_obligations',metadata,
    Column('team_id',String(36),nullable=False),Column('invocation_id',String(36),primary_key=True),
    Column('run_id',String(36),nullable=False),Column('thread_id',String(36),nullable=False),
    Column('job_id',String(36),nullable=False),Column('worker_id',String(128),nullable=False),
    Column('run_epoch',BigInteger,nullable=False),Column('daemon_id',String(128),nullable=False),Column('container_name',String(64),nullable=False),
    Column('volume_name',String(64),nullable=False),Column('status',String(24),nullable=False),
    Column('cleanup_owner',String(36)),Column('cleanup_until',BigInteger),Column('attempts',BigInteger,nullable=False),
    Column('next_retry_at',BigInteger,nullable=False),Column('creation_steps',BigInteger,nullable=False),
    Column('created_at',BigInteger,nullable=False),Column('updated_at',BigInteger,nullable=False),
    UniqueConstraint('team_id','invocation_id'),UniqueConstraint('container_name'),UniqueConstraint('volume_name'),
    ForeignKeyConstraint(['team_id','invocation_id'],['tool_invocations.team_id','tool_invocations.invocation_id'],ondelete='RESTRICT'),
    ForeignKeyConstraint(['team_id','run_id'],['runs.team_id','runs.run_id'],ondelete='RESTRICT'))

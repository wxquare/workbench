"""Immutable additive usage ledger snapshot (migration 0006)."""
from sqlalchemy import MetaData,Table,Column,String,Text,BigInteger,ForeignKeyConstraint,UniqueConstraint
from workbench_agent.persistence.schema_v4 import metadata as previous
metadata=MetaData(naming_convention=dict(previous.naming_convention))
for table in previous.tables.values(): table.to_metadata(metadata)
for column in (
    Column('kind',String(32),nullable=False,server_default='legacy'),
    Column('request_hash',String(64)),Column('guard_json',Text),
    Column('scope_id',String(36)),
    Column('model_attempts',BigInteger,nullable=False,server_default='0'),
    Column('tool_attempts',BigInteger,nullable=False,server_default='0'),
    Column('send_state',String(32),nullable=False,server_default='unsent')):
    metadata.tables['usage_reservations'].append_column(column)
for column in (
    Column('usage_pending',BigInteger,nullable=False,server_default='1'),
    Column('cost_pending',BigInteger,nullable=False,server_default='1')):
    metadata.tables['usage_records'].append_column(column)
usage_quarantine=Table('usage_quarantine',metadata,
    Column('team_id',String(36),nullable=False),Column('quarantine_id',String(36),primary_key=True),
    Column('run_id',String(36),nullable=False),Column('invocation',String(128),nullable=False),
    Column('receipt_hash',String(64),nullable=False),Column('payload',Text,nullable=False),
    Column('guard_json',Text,nullable=False),Column('status',String(32),nullable=False),
    Column('created_at',BigInteger,nullable=False),
    UniqueConstraint('run_id','invocation','receipt_hash'),
    ForeignKeyConstraint(['team_id','run_id'],['runs.team_id','runs.run_id'],ondelete='RESTRICT'))

usage_budget_policies=Table('usage_budget_policies',metadata,
    Column('team_id',String(36),nullable=False),Column('run_id',String(36),primary_key=True),
    Column('summary_tokens',BigInteger,nullable=False),Column('created_at',BigInteger,nullable=False),
    ForeignKeyConstraint(['team_id','run_id'],['runs.team_id','runs.run_id'],ondelete='RESTRICT'))

"""Authenticated synthetic alert metadata pinned to one tenant and Run."""
from sqlalchemy import MetaData, Table, Column, String, Text, BigInteger, ForeignKeyConstraint, UniqueConstraint
from workbench_agent.persistence.schema_v12 import metadata as previous
metadata=MetaData(naming_convention=dict(previous.naming_convention))
for table in previous.tables.values(): table.to_metadata(metadata)
synthetic_alert_sources=Table('synthetic_alert_sources',metadata,
    Column('team_id',String(36),nullable=False),Column('run_id',String(36),primary_key=True),
    Column('connector_id',String(128),nullable=False),Column('payload',Text,nullable=False),
    Column('payload_hash',String(64),nullable=False),Column('created_at',BigInteger,nullable=False),
    UniqueConstraint('team_id','run_id'),
    ForeignKeyConstraint(['team_id','run_id'],['runs.team_id','runs.run_id'],ondelete='RESTRICT'))

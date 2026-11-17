"""Immutable v2 additive snapshot; never mutate the v1 migration metadata graph."""
from sqlalchemy import MetaData, Table, Column, String, Text, BigInteger, ForeignKeyConstraint, UniqueConstraint, Index
from workbench_agent.persistence.schema_v1 import metadata as v1_metadata

metadata = MetaData(naming_convention=dict(v1_metadata.naming_convention))
for historical_table in v1_metadata.tables.values():
    historical_table.to_metadata(metadata)

team_idempotency_records = Table('team_idempotency_records', metadata,
    Column('team_id', String(36), nullable=False),
    Column('record_id', String(36), primary_key=True, nullable=False),
    Column('subject', String(128), nullable=False),
    Column('route', String(128), nullable=False),
    Column('key', String(128), nullable=False),
    Column('request_hash', String(64), nullable=False),
    Column('response_ref', Text, nullable=False),
    Column('status_code', BigInteger, nullable=False),
    Column('expires_at', BigInteger, nullable=False),
    Column('created_at', BigInteger, nullable=False, server_default='0'),
    Column('updated_at', BigInteger, nullable=False, server_default='0'),
    UniqueConstraint('team_id', 'record_id'),
    UniqueConstraint('team_id', 'subject', 'route', 'key'),
    ForeignKeyConstraint(['team_id'], ['teams.team_id'], ondelete='RESTRICT'),
    Index('ix_team_idempotency_expiry', 'expires_at'))

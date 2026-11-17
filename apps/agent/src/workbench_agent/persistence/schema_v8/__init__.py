"""Immutable Run and query-scope binding for uploaded artifacts."""
from sqlalchemy import MetaData, Table, Column, String, BigInteger, ForeignKeyConstraint, UniqueConstraint
from workbench_agent.persistence.schema_v7 import metadata as previous
metadata = MetaData(naming_convention=dict(previous.naming_convention))
for table in previous.tables.values():
    table.to_metadata(metadata)
artifact_upload_scopes = Table('artifact_upload_scopes', metadata,
    Column('team_id', String(36), nullable=False), Column('artifact_id', String(36), primary_key=True),
    Column('run_id', String(36), nullable=False), Column('scope_id', String(36)),
    Column('created_at', BigInteger, nullable=False),
    UniqueConstraint('team_id', 'artifact_id'),
    ForeignKeyConstraint(['team_id', 'artifact_id'], ['artifacts.team_id', 'artifacts.artifact_id'], ondelete='RESTRICT'),
    ForeignKeyConstraint(['team_id', 'run_id'], ['runs.team_id', 'runs.run_id'], ondelete='RESTRICT'))

"""Immutable SOP definitions; earlier schema snapshots remain unchanged."""
from sqlalchemy import MetaData, Table, Column, String, Text, BigInteger, ForeignKeyConstraint, UniqueConstraint
from workbench_agent.persistence.schema_v9 import metadata as previous

metadata = MetaData(naming_convention=dict(previous.naming_convention))
for table in previous.tables.values():
    table.to_metadata(metadata)
workflow_versions = Table('workflow_versions', metadata,
    Column('team_id', String(36), nullable=False),
    Column('workflow_version_id', String(36), primary_key=True),
    Column('workspace_id', String(36), nullable=False),
    Column('name', String(128), nullable=False),
    Column('version', BigInteger, nullable=False),
    Column('definition_json', Text, nullable=False),
    Column('hash', String(64), nullable=False),
    Column('created_at', BigInteger, nullable=False),
    Column('updated_at', BigInteger, nullable=False),
    UniqueConstraint('team_id', 'workflow_version_id'),
    UniqueConstraint('team_id', 'workspace_id', 'name', 'version'),
    ForeignKeyConstraint(['team_id', 'workspace_id'], ['workspaces.team_id', 'workspaces.workspace_id'], ondelete='RESTRICT'))

"""Current Agent schema, extending the immutable v1 migration snapshot."""
from sqlalchemy import BigInteger, Column, ForeignKeyConstraint, MetaData, String, Table, Text, UniqueConstraint
from workbench_agent.persistence.schema_agent_v1 import metadata as baseline

MIGRATION_HEAD = '0002_shared_skills'
metadata = MetaData(naming_convention=dict(baseline.naming_convention))
for table in baseline.tables.values():
    table.to_metadata(metadata)

skill_drafts = Table(
    'skill_drafts', metadata,
    Column('skill_id', String(36), primary_key=True, nullable=False),
    Column('team_id', String(36), nullable=False),
    Column('name', String(128), nullable=False),
    Column('description', Text, nullable=False),
    Column('content', Text, nullable=False),
    Column('created_at', BigInteger, nullable=False),
    Column('updated_at', BigInteger, nullable=False),
    UniqueConstraint('team_id', 'name'),
    ForeignKeyConstraint(['team_id', 'skill_id'], ['skills.team_id', 'skills.skill_id'], ondelete='RESTRICT'),
)

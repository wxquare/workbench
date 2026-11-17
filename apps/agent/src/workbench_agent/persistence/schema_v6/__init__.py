"""Tool binding drafts added without mutating historical snapshots."""
from sqlalchemy import MetaData,Table,Column,String,Text,BigInteger,ForeignKeyConstraint,UniqueConstraint
from workbench_agent.persistence.schema_v5 import metadata as previous
metadata=MetaData(naming_convention=dict(previous.naming_convention))
for table in previous.tables.values(): table.to_metadata(metadata)
tool_binding_drafts=Table('tool_binding_drafts',metadata,
    Column('team_id',String(36),nullable=False),Column('draft_id',String(36),primary_key=True),
    Column('workspace_id',String(36),nullable=False),Column('alias',String(128),nullable=False),
    Column('payload',Text,nullable=False),Column('revision',BigInteger,nullable=False),
    Column('created_at',BigInteger,nullable=False),Column('updated_at',BigInteger,nullable=False),
    UniqueConstraint('team_id','draft_id'),UniqueConstraint('team_id','workspace_id','alias'),
    ForeignKeyConstraint(['team_id','workspace_id'],['workspaces.team_id','workspaces.workspace_id'],ondelete='RESTRICT'))

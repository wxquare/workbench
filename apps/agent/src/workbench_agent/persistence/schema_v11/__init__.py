"""Durable history for manual schedule triggers."""
from sqlalchemy import MetaData,Table,Column,String,BigInteger,ForeignKeyConstraint,UniqueConstraint
from workbench_agent.persistence.schema_v10 import metadata as previous
metadata=MetaData(naming_convention=dict(previous.naming_convention))
for table in previous.tables.values(): table.to_metadata(metadata)
schedule_manual_fires=Table('schedule_manual_fires',metadata,
    Column('team_id',String(36),nullable=False),Column('fire_id',String(36),primary_key=True),
    Column('schedule_id',String(36),nullable=False),Column('run_id',String(36),nullable=False),
    Column('actor',String(128),nullable=False),Column('created_at',BigInteger,nullable=False),
    UniqueConstraint('team_id','fire_id'),UniqueConstraint('team_id','run_id'),
    ForeignKeyConstraint(['team_id','schedule_id'],['schedules.team_id','schedules.schedule_id'],ondelete='RESTRICT'),
    ForeignKeyConstraint(['team_id','run_id'],['runs.team_id','runs.run_id'],ondelete='RESTRICT'))

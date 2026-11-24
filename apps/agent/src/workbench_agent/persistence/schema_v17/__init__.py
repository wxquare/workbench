"""Provenance binding between incident links and acknowledged actions."""

from sqlalchemy import MetaData, Table, Column, String, UniqueConstraint, ForeignKeyConstraint
from workbench_agent.persistence.schema_v16 import metadata as previous

metadata = MetaData(naming_convention=dict(previous.naming_convention))
for table in previous.tables.values():
    table.to_metadata(metadata)
incident_action_links = Table(
    'incident_action_links',
    metadata,
    Column('team_id', String(36), nullable=False),
    Column('link_id', String(36), primary_key=True),
    Column('action_id', String(36), nullable=False),
    UniqueConstraint('team_id', 'link_id'),
    UniqueConstraint('team_id', 'action_id'),
    ForeignKeyConstraint(
        ['team_id', 'link_id'],
        ['incident_links.team_id', 'incident_links.link_id'],
        ondelete='RESTRICT',
    ),
    ForeignKeyConstraint(
        ['team_id', 'action_id'], ['actions.team_id', 'actions.action_id'], ondelete='RESTRICT'
    ),
)

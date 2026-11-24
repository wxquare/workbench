"""Identity binding for synthetic result candidates and safe projections."""

from sqlalchemy import MetaData, Table, Column, String, ForeignKeyConstraint, UniqueConstraint
from workbench_agent.persistence.schema_v13 import metadata as previous

metadata = MetaData(naming_convention=dict(previous.naming_convention))
for table in previous.tables.values():
    table.to_metadata(metadata)
synthetic_result_bindings = Table(
    'synthetic_result_bindings',
    metadata,
    Column('team_id', String(36), nullable=False),
    Column('run_id', String(36), primary_key=True),
    Column('candidate_digest', String(64), nullable=False),
    Column('projection_digest', String(64), nullable=False),
    UniqueConstraint('team_id', 'run_id'),
    ForeignKeyConstraint(
        ['team_id', 'run_id'], ['runs.team_id', 'runs.run_id'], ondelete='RESTRICT'
    ),
)

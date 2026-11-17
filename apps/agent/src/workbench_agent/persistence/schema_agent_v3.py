"""Agent schema with durable, minimal trace propagation."""
from sqlalchemy import BigInteger, Column, ForeignKeyConstraint, MetaData, String, Table
from workbench_agent.persistence.schema_agent_v2 import metadata as baseline

MIGRATION_HEAD = '0003_run_trace_contexts'
metadata = MetaData(naming_convention=dict(baseline.naming_convention))
for table in baseline.tables.values():
    table.to_metadata(metadata)

run_trace_contexts = Table(
    'run_trace_contexts', metadata,
    Column('run_id', String(36), primary_key=True, nullable=False),
    Column('team_id', String(36), nullable=False),
    Column('traceparent', String(55), nullable=False),
    Column('created_at', BigInteger, nullable=False),
    ForeignKeyConstraint(['team_id', 'run_id'], ['runs.team_id', 'runs.run_id'], ondelete='RESTRICT'),
)

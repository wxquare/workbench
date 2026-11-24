"""Current Agent schema, with semantic graph IDs on runtime profiles."""

from sqlalchemy import Column, MetaData, String
from workbench_agent.persistence.schema_agent_v4 import metadata as baseline

MIGRATION_HEAD = '0005_graph_id'
metadata = MetaData(naming_convention=dict(baseline.naming_convention))
for table in baseline.tables.values():
    table.to_metadata(metadata)

profiles = metadata.tables['runtime_profiles']
profiles._columns.remove(profiles.c.graph_version)
profiles.append_column(Column('graph_id', String(128), nullable=False))

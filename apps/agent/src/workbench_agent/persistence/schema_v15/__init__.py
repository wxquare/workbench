"""Pin the precise deployment identity used by a synthetic source."""

from sqlalchemy import MetaData, Column, String
from workbench_agent.persistence.schema_v14 import metadata as previous

metadata = MetaData(naming_convention=dict(previous.naming_convention))
for table in previous.tables.values():
    table.to_metadata(metadata)
synthetic_alert_sources = metadata.tables['synthetic_alert_sources']
# Historical sources have no identity proof and remain unavailable.
synthetic_alert_sources.append_column(Column('identity_id', String(128), nullable=True))

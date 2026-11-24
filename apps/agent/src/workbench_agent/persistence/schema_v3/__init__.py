"""Immutable additive durable timing snapshot."""

from sqlalchemy import MetaData, Column, BigInteger
from workbench_agent.persistence.schema_v2 import metadata as previous

metadata = MetaData(naming_convention=dict(previous.naming_convention))
for table in previous.tables.values():
    table.to_metadata(metadata)
runs = metadata.tables['runs']
for column in (
    Column('started_at', BigInteger),
    Column('active_elapsed_ms', BigInteger, nullable=False, server_default='0'),
    Column('active_since', BigInteger),
):
    runs.append_column(column)

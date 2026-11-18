"""Current public SQLAlchemy schema. Only maintenance migrations execute DDL.

Immutable versioned modules preserve historical migration snapshots.
Application code consumes Store ports; SQLAlchemy tables belong to adapters.
"""
from workbench_agent.persistence.schema_agent_v4 import metadata

# Explicitly expose table names without duplicating schema declarations.
globals().update(metadata.tables)
REQUIRED_TABLES = frozenset(metadata.tables)

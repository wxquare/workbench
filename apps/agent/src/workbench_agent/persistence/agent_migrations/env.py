"""Agent-only maintenance environment using an explicitly supplied connection."""
from alembic import context
from workbench_agent.persistence.schema_agent_v3 import metadata

connection = context.config.attributes.get('connection')
if connection is None:
    raise RuntimeError('use workbench db migrate with an explicit validated configuration')
context.configure(connection=connection, target_metadata=metadata, render_as_batch=True)
with context.begin_transaction():
    context.run_migrations()

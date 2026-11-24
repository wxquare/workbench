"""Explicit knowledge index inspection and repair commands."""

from workbench_agent.knowledge.maintenance import KnowledgeMaintenance
from workbench_agent.persistence.backends.sqlite import SQLiteBackend


async def maintain(settings, command, team_id, *, entry_id=None, expected_revision=None):
    backend = SQLiteBackend(settings.storage.database_url.get_secret_value())
    try:
        if not await backend.health():
            raise RuntimeError('knowledge storage unavailable')
        maintenance = KnowledgeMaintenance(backend)
        if command == 'check-index':
            return await maintenance.check(team_id)
        return await maintenance.rebuild(team_id, entry_id, expected_revision)
    finally:
        await backend.close()

"""Explicit local maintenance sync; no service startup changes policy."""
from workbench_agent.application.action_grants import ActionGrantService
from workbench_agent.persistence.backends.sqlite import SQLiteBackend
from workbench_agent.persistence.readiness import storage_ready

async def sync_action_policy(settings,expected_revision):
    backend=SQLiteBackend(settings.storage.database_url.get_secret_value())
    try:
        if not await storage_ready(settings,backend): return {'ready':False,'status':'storage_unavailable'}
        revision=await ActionGrantService(backend).sync(
            [g.model_dump(mode='json') for g in settings.service.action_grants],
            settings.service.tool_permissions,settings.service.connector_identities,expected_revision=expected_revision)
        return {'ready':True,'revision':revision}
    finally:
        await backend.close()

async def action_policy_status(settings):
    backend=SQLiteBackend(settings.storage.database_url.get_secret_value())
    try:
        if not await storage_ready(settings,backend): return {'ready':False,'status':'storage_unavailable'}
        async with backend.create_uow().begin_read() as uow:
            row=await backend.actions.policy(uow)
        return {'ready':True,'revision':row['revision'] if row else 0,'policy_hash':row['policy_hash'] if row else None}
    finally:
        await backend.close()

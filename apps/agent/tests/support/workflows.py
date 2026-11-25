"""Create immutable workflow records for tests requiring non-seed definitions."""

from uuid import uuid4

from workbench_agent.application.idempotency import canonical, digest
from workbench_agent.domain.workflows import WorkflowDefinition, WorkflowVersion


async def create_workflow_version(platform, definition):
    definition = WorkflowDefinition.model_validate(definition.model_dump(mode='json'))
    data = definition.model_dump(mode='json')
    async with platform.backend.create_uow().begin_write() as uow:
        version = 1 + await platform.backend.configuration.latest_workflow_version(
            uow, platform.auth.team_id, platform.agent_id, definition.name
        )
        now = await platform.backend.clock.now_ms(uow)
        result = WorkflowVersion(
            workflow_version_id=str(uuid4()),
            agent_id=platform.agent_id,
            version=version,
            content_hash=digest(data),
            definition=definition,
        )
        await platform.backend.configuration.insert(
            uow,
            'workflow_versions',
            {
                'team_id': platform.auth.team_id,
                'workflow_version_id': result.workflow_version_id,
                'agent_id': platform.agent_id,
                'name': definition.name,
                'version': version,
                'definition_json': canonical(data),
                'hash': result.content_hash,
                'created_at': now,
                'updated_at': now,
            },
        )
    return result

"""Publish immutable workflow versions under agent authorization."""
import json
from uuid import uuid4
from workbench_agent.application.authorization import Authorization
from workbench_agent.application.errors import not_found
from workbench_agent.application.idempotency import Idempotency, canonical, digest
from workbench_agent.domain.workflows import WorkflowDefinition, WorkflowVersion

class WorkflowService:
    def __init__(self, backend):
        self.backend = backend
        self.authz = Authorization(backend)
        self.idempotency = Idempotency(backend)

    async def publish(self, auth, agent_id, definition: WorkflowDefinition, *, key):
        data = definition.model_dump(mode='json')
        async with self.backend.create_uow().begin_write() as uow:
            await self.authz.current(uow, auth, agent_id, 'maintainer')
            async def operation():
                version = 1 + await self.backend.configuration.latest_workflow_version(
                    uow, auth.team_id, agent_id, definition.name)
                now = await self.backend.clock.now_ms(uow)
                result = WorkflowVersion(workflow_version_id=str(uuid4()), agent_id=agent_id,
                    version=version, content_hash=digest(data), definition=definition)
                await self.backend.configuration.insert(uow, 'workflow_versions', dict(team_id=auth.team_id,
                    workflow_version_id=result.workflow_version_id, agent_id=agent_id, name=definition.name,
                    version=version, definition_json=canonical(data), hash=result.content_hash, created_at=now, updated_at=now))
                return result.model_dump(mode='json')
            response = await self.idempotency.execute(uow, auth, agent_id, 'workflow.publish', key, data, operation, status=201)
        return WorkflowVersion.model_validate(response.body)

    async def get(self, auth, agent_id, workflow_version_id):
        async with self.backend.create_uow().begin_read() as uow:
            await self.authz.current(uow, auth, agent_id)
            row = await self.backend.configuration.one(uow, 'workflow_versions', team_id=auth.team_id,
                agent_id=agent_id, workflow_version_id=workflow_version_id)
            if not row:
                raise not_found()
            data = json.loads(row['definition_json'])
            if digest(data) != row['hash']:
                raise not_found()
            return WorkflowVersion(workflow_version_id=row['workflow_version_id'], agent_id=agent_id,
                version=row['version'], content_hash=row['hash'], definition=WorkflowDefinition.model_validate(data))

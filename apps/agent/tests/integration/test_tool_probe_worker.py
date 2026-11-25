"""Tool probing is fenced by current authority and the management-job lease."""

import json

import pytest
from sqlalchemy import update

from workbench_agent.application.errors import DomainError
from workbench_agent.domain.admin_jobs import AdminAuth
from workbench_agent.domain.tools import ToolBinding
from workbench_agent.jobs.admin import AdminJobService
from workbench_agent.jobs.worker import ExecutorRegistry, Worker
from workbench_agent.persistence import schema
from workbench_agent.persistence.errors import StorageError
from workbench_agent.tools.mcp import ToolCatalog
from workbench_agent.tools.probe import ToolProbe
from workbench_agent.tools.registry import ToolRegistry


class ProbeAdapter:
    def __init__(self):
        self.discoveries = 0
        self.calls = 0

    async def discover(self, *args, **kwargs):
        self.discoveries += 1
        return ToolCatalog(
            ('local.logs',),
            (
                {
                    'name': 'local.logs',
                    'inputSchema': {
                        'type': 'object',
                        'properties': {'service': {'type': 'integer'}},
                    },
                },
            ),
        )

    async def call(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError('unexpected tool invocation')


async def enqueue(platform):
    binding = ToolBinding(
        alias='logs.search',
        provider='mcp',
        tool_name='local.logs',
        server='logs',
        credential_scope='reader',
        permission='logs.read',
        input_schema={'type': 'object', 'properties': {'service': {'type': 'string'}}},
    )
    registry = ToolRegistry(platform.backend)
    await registry.save_draft(
        platform.auth, platform.agent_id, binding, expected_revision=0, key='draft'
    )
    return await registry.enqueue_probe(
        platform.auth, platform.agent_id, binding.alias, key='probe'
    )


@pytest.mark.asyncio
async def test_worker_reports_schema_mismatch_without_invocation(platform):
    job = await enqueue(platform)
    adapter = ProbeAdapter()
    request_key = (platform.auth.team_id, platform.agent_id, 'logs', 'reader', 'local.logs')
    registry = ExecutorRegistry()
    registry.register_admin(
        'tool_probe',
        ToolProbe(
            platform.backend, adapter, probe_requests={request_key: {'service': 'x'}}
        ).execute,
    )
    assert await Worker(platform.backend, 'worker', registry).run_admin_once() == 'admin_completed'
    auth = AdminAuth(platform.auth.team_id, platform.agent_id, platform.auth.subject, True)
    report = json.loads((await AdminJobService(platform.backend).get(auth, job.job_id)).result_ref)
    assert (report['discovery'], report['schema'], report['invocation']) == (
        'ok',
        'mismatch',
        'not_run',
    )
    assert adapter.discoveries == 1 and adapter.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['revoked', 'expired'])
async def test_probe_denies_lost_authority_before_discovery(platform, reason):
    job = await enqueue(platform)
    async with platform.backend.create_uow().begin_write() as uow:
        claim = await platform.backend.admin_jobs.claim(uow, {'tool_probe'}, 'worker')
        if reason == 'expired':
            await uow.execute(
                update(schema.admin_jobs)
                .where(schema.admin_jobs.c.job_id == job.job_id)
                .values(lease_until=0)
            )
    if reason == 'revoked':
        await platform.teams.set_member(
            platform.auth,
            platform.auth.subject,
            'admin',
            'revoked',
            expected_revision=0,
            key='revoke',
        )
    adapter = ProbeAdapter()
    with pytest.raises(DomainError if reason == 'revoked' else StorageError):
        await ToolProbe(platform.backend, adapter).execute(claim)
    assert adapter.discoveries == 0 and adapter.calls == 0

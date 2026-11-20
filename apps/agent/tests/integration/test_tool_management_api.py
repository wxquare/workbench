from types import SimpleNamespace
from uuid import uuid4

import pytest

from workbench_agent.application.errors import DomainError
from workbench_agent.tools.management import ToolManagement


def deployment(team_id, agent_id):
    return SimpleNamespace(
        server='local',
        credential_scope='reader',
        read_tools=('knowledge_search', 'test_read_counter'),
        team_id=team_id,
        agent_id=agent_id,
        endpoint='https://private.example.invalid/mcp',
        secret_ref='env://PRIVATE_TOKEN',
        mapping_policies=(
            SimpleNamespace(
                tool_name='test_read_counter',
                model_dump=lambda mode='json': {
                    'tool_name': 'test_read_counter',
                    'mapping_hash': 'a' * 64,
                    'model_input_fields': ['query'],
                    'protected_targets': [['scope', 'team']],
                },
            ),
        ),
        tool_conversation_allowed_tools=('test_read_counter',),
    )


@pytest.mark.asyncio
async def test_connection_projection_is_admin_scoped_and_redacted(platform, client):
    service = ToolManagement(
        platform.backend,
        [deployment(platform.auth.team_id, platform.agent_id)],
    )
    result = await service.connections(platform.auth, platform.agent_id)
    assert result['items'] == [{
        'server': 'local',
        'credential_scope': 'reader',
        'read_tools': ['knowledge_search', 'test_read_counter'],
        'status': 'configured',
        'tool_conversation_tools': ['test_read_counter'],
        'mapping_policies': [{
            'tool_name': 'test_read_counter',
            'mapping_hash': 'a' * 64,
            'model_input_fields': ['query'],
            'protected_targets': [['scope', 'team']],
        }],
    }]
    assert 'private.example' not in str(result)
    assert 'PRIVATE_TOKEN' not in str(result)
    path = f'/v1/agents/{platform.agent_id}/tool-connections'
    response = await client.get(path)
    assert response.status_code == 200
    with pytest.raises(DomainError) as error:
        await service.connections(platform.other_auth, platform.agent_id)
    assert error.value.status in (403, 404)


@pytest.mark.asyncio
async def test_discovery_requires_exact_deployment_binding(platform, client, monkeypatch):
    service = ToolManagement(
        platform.backend,
        [deployment(platform.auth.team_id, platform.agent_id)],
    )
    with pytest.raises(DomainError):
        await service.enqueue_discovery(
            platform.auth,
            platform.agent_id,
            server='other',
            credential_scope='reader',
            key='discover-missing',
        )


@pytest.mark.asyncio
async def test_http_binding_management_rejects_write_binding(platform, client):
    binding = {
        'alias': 'write',
        'provider': 'mcp',
        'tool_name': 'write',
        'server': 'local',
        'credential_scope': 'reader',
        'permission': 'write',
        'input_schema': {'type': 'object'},
        'read_only': False,
    }
    response = await client.put(
        f'/v1/agents/{platform.agent_id}/tool-bindings/write',
        json=binding,
        headers={'If-Match': '0', 'Idempotency-Key': str(uuid4())},
    )
    assert response.status_code == 422
    assert response.json()['code'] == 'WRITE_BINDING_HTTP_DISABLED'


@pytest.mark.asyncio
async def test_binding_list_exposes_only_publish_projections_needed_by_agent_editor(
    platform, client
):
    from workbench_agent.domain.tools import ToolBinding
    from workbench_agent.tools.registry import ToolRegistry

    binding = ToolBinding(
        alias='knowledge.search',
        provider='mcp',
        tool_name='knowledge_search',
        server='local',
        credential_scope='reader',
        permission='knowledge.read',
        input_schema={'type': 'object'},
    )
    await ToolRegistry(platform.backend).publish(
        platform.auth, platform.agent_id, binding, key='binding-list'
    )
    response = await client.get(
        f'/v1/agents/{platform.agent_id}/tool-bindings?limit=100'
    )

    assert response.status_code == 200
    item = response.json()['items'][0]
    assert item['alias'] == 'knowledge.search'
    assert item['tool_name'] == 'knowledge_search'
    assert item['read_only'] is True
    assert item['tool_conversation_compatible'] is True
    assert item['latest_ref']['version'] == '1'


@pytest.mark.asyncio
async def test_tool_discovery_worker_filters_unlisted_tools(platform):
    from workbench_agent.domain.admin_jobs import AdminAuth
    from workbench_agent.jobs.admin import AdminJobService
    from workbench_agent.jobs.worker import ExecutorRegistry, Worker
    from workbench_agent.tools.management import ToolDiscovery
    from workbench_agent.tools.mcp import ToolCatalog

    job = await AdminJobService(platform.backend).submit(
        AdminAuth(platform.auth.team_id, platform.agent_id, platform.auth.subject, True),
        'tool_discovery',
        {
            'server': 'local',
            'credential_scope': 'reader',
            'subject': platform.auth.subject,
            'identity_kind': platform.auth.identity_kind,
            'agent_scope': platform.auth.agent_scope,
        },
        'tool-discovery-job',
    )
    class Adapter:
        async def discover(self, *args, **kwargs):
            return ToolCatalog(
                ('knowledge_search', 'hidden_write'),
                (
                    {'name': 'knowledge_search', 'description': 'query', 'inputSchema': {'type': 'object'}},
                    {'name': 'hidden_write', 'description': 'write', 'inputSchema': {'type': 'object'}},
                ),
            )
    registry = ExecutorRegistry()
    registry.register_admin(
        'tool_discovery',
        ToolDiscovery(
            platform.backend,
            Adapter(),
            [deployment(platform.auth.team_id, platform.agent_id)],
        ).execute,
    )
    assert await Worker(platform.backend, 'worker', registry).run_admin_once() == 'admin_completed'
    result = await AdminJobService(platform.backend).get(
        AdminAuth(platform.auth.team_id, platform.agent_id, platform.auth.subject, True),
        job.job_id,
    )
    assert 'knowledge_search' in result.result_ref
    assert 'hidden_write' not in result.result_ref
    assert '"tool_conversation_compatible":true' in result.result_ref
    assert 'private.example' not in result.result_ref
    assert 'PRIVATE_TOKEN' not in result.result_ref

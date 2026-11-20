import pytest

from workbench_agent.application.errors import DomainError
from workbench_agent.domain.tools import ToolBinding
from workbench_agent.settings import MCPConnectionBinding
from workbench_agent.tools.registry import ToolRegistry
from workbench_agent.tools.scope_mapping import (
    ArgumentMapping,
    FieldMapping,
    mapping_hash,
)


FULL_SCHEMA = {
    'type': 'object',
    'additionalProperties': False,
    'properties': {
        'query': {'type': 'string'},
        'scope': {
            'type': 'object',
            'additionalProperties': False,
            'properties': {
                'team': {'type': 'string'},
                'environment': {'type': 'string'},
                'region': {'type': 'string'},
                'service': {'type': 'string'},
                'source_id': {'type': 'string'},
            },
            'required': ['team', 'environment', 'region', 'service', 'source_id'],
        },
        'context': {
            'type': 'object',
            'additionalProperties': False,
            'properties': {
                'run_id': {'type': 'string'},
                'invocation_id': {'type': 'string'},
                'query_scope_ref': {'type': 'string'},
            },
            'required': ['run_id', 'invocation_id', 'query_scope_ref'],
        },
    },
    'required': ['query', 'scope', 'context'],
}
SCOPE = {
    'service': 'checkout',
    'environment': 'test',
    'region': 'id',
    'start_at': '2026-11-20T00:00:00Z',
    'end_at': '2026-11-20T00:10:00Z',
}


def mapping():
    return ArgumentMapping(
        fields=(
            FieldMapping(origin='model', source='query', target=('query',)),
            FieldMapping(origin='query_scope', source='environment', target=('scope', 'environment')),
            FieldMapping(origin='query_scope', source='region', target=('scope', 'region')),
            FieldMapping(origin='query_scope', source='service', target=('scope', 'service')),
            FieldMapping(origin='grant', source='team', target=('scope', 'team')),
            FieldMapping(origin='grant', source='sources.test_read_counter', target=('scope', 'source_id')),
            FieldMapping(origin='context', source='run_id', target=('context', 'run_id')),
            FieldMapping(origin='context', source='invocation_id', target=('context', 'invocation_id')),
            FieldMapping(origin='context', source='query_scope_ref', target=('context', 'query_scope_ref')),
        ),
        model_input_schema={
            'type': 'object',
            'additionalProperties': False,
            'properties': {'query': {'type': 'string'}},
            'required': ['query'],
        },
    )


def policy_payload(value=None):
    value = value or mapping()
    return {
        'tool_name': 'test_read_counter',
        'mapping_hash': mapping_hash(value),
        'model_input_fields': ['query'],
        'query_scope_fields': ['environment', 'region', 'service'],
        'grant_fields': ['team', 'sources.test_read_counter'],
        'context_fields': ['run_id', 'invocation_id', 'query_scope_ref'],
        'protected_targets': [
            ['scope', 'team'],
            ['scope', 'environment'],
            ['scope', 'region'],
            ['scope', 'service'],
            ['scope', 'source_id'],
            ['context', 'run_id'],
            ['context', 'invocation_id'],
            ['context', 'query_scope_ref'],
        ],
    }


def connection(platform, *, value=None):
    return MCPConnectionBinding.model_validate({
        'server': 'local',
        'credential_scope': 'read',
        'team_id': platform.auth.team_id,
        'agent_id': platform.agent_id,
        'endpoint': 'https://example.invalid/mcp',
        'secret_ref': 'env://MCP_TEST',
        'read_tools': ['test_read_counter'],
        'mapping_policies': [policy_payload(value)],
        'tool_conversation_allowed_tools': ['test_read_counter'],
    })


def binding(value=None):
    return ToolBinding(
        alias='counter.read',
        provider='mcp',
        tool_name='test_read_counter',
        server='local',
        credential_scope='read',
        permission='counter.read',
        input_schema=FULL_SCHEMA,
        read_only=True,
        argument_mapping=value or mapping(),
    )


def test_deployment_mapping_policy_is_pinnable_and_bounded(platform):
    deployment = connection(platform)
    assert deployment.mapping_policies[0].mapping_hash == mapping_hash(mapping())
    assert deployment.tool_conversation_allowed_tools == ('test_read_counter',)
    assert deployment.mapping_policy_for('test_read_counter') is not None
    assert deployment.mapping_policy_for('unknown') is None


def test_deployment_rejects_unpinned_or_unknown_mapping_configuration(platform):
    data = connection(platform).model_dump(mode='json')
    data['tool_conversation_allowed_tools'] = ['unknown']
    with pytest.raises(ValueError):
        MCPConnectionBinding.model_validate(data)


def test_configured_policy_normalizes_mapping_metadata_for_runtime(platform):
    from workbench_agent.tools.mcp import configured_connection_policy

    connection_policy = configured_connection_policy(
        lambda: (connection(platform),),
    )
    rule = connection_policy._bindings()[0]

    assert rule.mapping_policies == (
        {**policy_payload(), 'schema_hash': None},
    )
    assert rule.tool_conversation_allowed_tools == frozenset({'test_read_counter'})


async def publish(platform, value):
    deployment = connection(platform)
    registry = ToolRegistry(platform.backend, mcp_connections=(deployment,))
    return await registry.publish(
        platform.auth,
        platform.agent_id,
        value,
        key='scope-map-publish',
        managed=True,
    )


@pytest.mark.asyncio
async def test_registry_rejects_mapping_without_matching_deployment_policy(platform):
    changed = mapping().model_copy(update={
        'fields': (*mapping().fields[:-1], FieldMapping(
            origin='context',
            source='run_id',
            target=('context', 'query_scope_ref'),
        )),
    })
    with pytest.raises(DomainError) as error:
        await publish(platform, binding(changed))
    assert error.value.code == 'TOOL_MAPPING_DENIED'


@pytest.mark.asyncio
async def test_registry_accepts_policy_pinned_mapping(platform):
    ref = await publish(platform, binding())
    assert len(ref.content_hash) == 64


@pytest.mark.asyncio
async def test_profile_rejects_mapped_tool_when_deployment_policy_is_revoked(platform):
    ref = await publish(platform, binding())
    from workbench_agent.domain.profiles import ProfileDraft
    from workbench_agent.runtime.graphs.ids import GraphId

    context = await platform.contexts.resolve(platform.auth, platform.agent_id)
    agent = await platform.profiles.get_agent(platform.auth, platform.agent_id)
    platform.backend._mcp_connections = ()
    from workbench_agent.application.profiles import ProfileService
    service = ProfileService(platform.backend)
    with pytest.raises(DomainError) as error:
        await service.save_draft(
            platform.auth,
            platform.agent_id,
            ProfileDraft(
                default_model=context.default_model,
                graph_id=GraphId.CONVERSATION_TOOL_USE,
                allowed_modes=('qa',),
                tools=(ref,),
            ),
            agent['revision'],
            key='revoked-mapping-profile',
        )
    assert error.value.code == 'TOOL_BINDINGS_INVALID'


@pytest.mark.asyncio
async def test_profile_accepts_mapped_tool_only_while_policy_is_current(platform):
    ref = await publish(platform, binding())
    platform.backend._mcp_connections = (connection(platform),)
    from workbench_agent.domain.profiles import ProfileDraft
    from workbench_agent.runtime.graphs.ids import GraphId

    context = await platform.contexts.resolve(platform.auth, platform.agent_id)
    agent = await platform.profiles.get_agent(platform.auth, platform.agent_id)
    saved = await platform.profiles.save_draft(
        platform.auth,
        platform.agent_id,
        ProfileDraft(
            default_model=context.default_model,
            graph_id=GraphId.CONVERSATION_TOOL_USE,
            allowed_modes=('qa',),
            tools=(ref,),
        ),
        agent['revision'],
        key='current-mapping-profile',
    )
    assert saved['draft']['tools'][0]['resource_id'] == ref.resource_id

    platform.backend._mcp_connections = ()
    with pytest.raises(DomainError):
        await platform.profiles.publish(
            platform.auth,
            platform.agent_id,
            saved['revision'],
            key='current-mapping-profile-publish',
        )


@pytest.mark.asyncio
async def test_configuration_policy_adapter_accepts_only_allowlisted_mapped_tool(platform):
    from workbench_agent.settings import Settings
    from workbench_agent.tools.mcp import configured_adapter

    data = connection(platform).model_dump(mode='json')
    settings = Settings.model_validate({
        'asset_root': str(platform.settings.asset_root),
        'storage': {
            'database_url': platform.settings.storage.database_url.get_secret_value(),
            'persistent_root': str(platform.settings.storage.persistent_root),
        },
        'service': {'mcp_connections': [data]},
    })
    class Secrets:
        def resolve(self, ref):
            return 'fictional-secret'

    adapter = configured_adapter(settings.service.mcp_connections, Secrets())
    assert adapter is not None
    adapter.policy.require(
        platform.auth.team_id,
        platform.agent_id,
        'local',
        'read',
        'test_read_counter',
    )
    with pytest.raises(PermissionError):
        adapter.policy.require(
            platform.auth.team_id,
            platform.agent_id,
            'local',
            'read',
            'unlisted_tool',
        )


@pytest.mark.asyncio
async def test_http_binding_api_uses_current_mapping_policy(platform, client):
    data = binding().model_dump(mode='json')
    data['permission'] = 'counter.read'
    response = await client.put(
        f'/v1/agents/{platform.agent_id}/tool-bindings/{data["alias"]}',
        json=data,
        headers={'If-Match': '0', 'Idempotency-Key': 'api-mapped-draft'},
    )
    assert response.status_code == 422
    assert response.json()['code'] == 'TOOL_MAPPING_DENIED'


@pytest.mark.asyncio
async def test_internal_scope_mapped_publish_still_requires_deployment_policy(platform):
    with pytest.raises(DomainError) as error:
        await ToolRegistry(platform.backend).publish(
            platform.auth,
            platform.agent_id,
            binding(),
            key='unpinned-internal-mapping',
        )
    assert error.value.code == 'TOOL_MAPPING_DENIED'

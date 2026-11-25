from uuid import uuid4

import pytest

from contract.test_scoped_mcp_mapping import SCOPE
from support.runs import make_run_payload
from workbench_agent.domain.profiles import ProfileDraft
from workbench_agent.domain.tools import ToolBinding
from workbench_agent.runtime.graphs.ids import GraphId
from workbench_agent.tools.registry import ToolRegistry


async def publish_profile(platform, tools):
    current = await platform.contexts.resolve(platform.auth, platform.agent_id)
    agent = await platform.profiles.get_agent(platform.auth, platform.agent_id)
    saved = await platform.profiles.save_draft(
        platform.auth,
        platform.agent_id,
        ProfileDraft(
            default_model=current.default_model,
            graph_id=GraphId.CONVERSATION_TOOL_USE,
            allowed_modes=('qa',),
            tools=tools,
        ),
        agent['revision'],
        key='tool-use-profile',
    )
    await platform.profiles.publish(
        platform.auth,
        platform.agent_id,
        saved['revision'],
        key='tool-use-profile-publish',
    )


async def knowledge_ref(platform):
    binding = ToolBinding(
        alias='knowledge.search',
        provider='mcp',
        tool_name='knowledge_search',
        server='local',
        credential_scope='read',
        permission='knowledge.read',
        input_schema={'type': 'object', 'properties': {'query': {'type': 'string'}}},
    )
    return await ToolRegistry(platform.backend).publish(
        platform.auth, platform.agent_id, binding, key='tool-use'
    )


@pytest.mark.asyncio
async def test_tool_conversation_admits_scoped_published_knowledge_search(platform, client):
    ref = await knowledge_ref(platform)
    await publish_profile(platform, (ref,))
    payload = make_run_payload(platform.agent_id)
    payload['business_context'] = {
        'conversation': True,
        'query_scope': SCOPE,
    }
    response = await client.post(
        '/v1/runs', json=payload, headers={'Idempotency-Key': str(uuid4())}
    )
    assert response.status_code == 202, response.text
    view = (await client.get('/v1/runs/' + response.json()['run_id'])).json()
    assert view['status'] == 'queued'


@pytest.mark.asyncio
async def test_configured_mapped_tool_requires_its_current_policy(platform, client):
    from workbench_agent.tools.registry import ToolRegistry
    from test_scope_mapping_policy import binding, connection

    deployment = connection(platform)
    registry = ToolRegistry(
        platform.backend,
        mcp_connections=(deployment,),
    )
    ref = await registry.publish(
        platform.auth,
        platform.agent_id,
        binding(),
        key='admit-configured-tool',
    )
    platform.backend._mcp_connections = (deployment,)
    await publish_profile(platform, (ref,))
    payload = make_run_payload(platform.agent_id)
    payload['business_context'] = {
        'conversation': True,
        'query_scope': SCOPE,
    }
    accepted = await client.post(
        '/v1/runs',
        json=payload,
        headers={'Idempotency-Key': 'admit-configured-run'},
    )
    assert accepted.status_code == 202, accepted.text


@pytest.mark.asyncio
async def test_tool_conversation_requires_scope_and_read_only_supported_binding(platform, client):
    ref = await knowledge_ref(platform)
    await publish_profile(platform, (ref,))
    payload = make_run_payload(platform.agent_id)
    payload['business_context'] = {'conversation': True}
    response = await client.post(
        '/v1/runs', json=payload, headers={'Idempotency-Key': 'missing-scope'}
    )
    assert response.status_code == 422

    write = ToolBinding(
        alias='knowledge.write',
        provider='mcp',
        tool_name='knowledge_search',
        server='local',
        credential_scope='read',
        permission='knowledge.read',
        input_schema={'type': 'object'},
        read_only=False,
    )
    write_ref = await ToolRegistry(platform.backend).publish(
        platform.auth, platform.agent_id, write, key='write-ref'
    )
    current = await platform.contexts.resolve(platform.auth, platform.agent_id)
    agent = await platform.profiles.get_agent(platform.auth, platform.agent_id)
    with pytest.raises(Exception):
        await platform.profiles.save_draft(
            platform.auth,
            platform.agent_id,
            ProfileDraft(
                default_model=current.default_model,
                graph_id=GraphId.CONVERSATION_TOOL_USE,
                tools=(write_ref,),
            ),
            agent['revision'],
            key='write-profile',
        )


@pytest.mark.asyncio
async def test_tool_conversation_rejects_more_than_four_tools(platform):
    refs = []
    for index in range(5):
        binding = ToolBinding(
            alias=f'knowledge.search{index}',
            provider='mcp',
            tool_name='knowledge_search',
            server='local',
            credential_scope='read',
            permission='knowledge.read',
            input_schema={'type': 'object'},
        )
        refs.append(
            await ToolRegistry(platform.backend).publish(
                platform.auth,
                platform.agent_id,
                binding,
                key=f'tool-{index}',
            )
        )
    current = await platform.contexts.resolve(platform.auth, platform.agent_id)
    agent = await platform.profiles.get_agent(platform.auth, platform.agent_id)
    with pytest.raises(Exception):
        await platform.profiles.save_draft(
            platform.auth,
            platform.agent_id,
            ProfileDraft(
                default_model=current.default_model,
                graph_id=GraphId.CONVERSATION_TOOL_USE,
                tools=tuple(refs),
            ),
            agent['revision'],
            key='too-many-tools',
        )

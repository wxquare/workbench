import json
from types import SimpleNamespace

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from contract.test_dod_mcp_mapping import GRANT, SCOPE
from support.runs import make_run_payload
from workbench_agent.domain.profiles import ProfileDraft
from workbench_agent.domain.tools import ToolBinding
from workbench_agent.jobs.worker import Worker
from workbench_agent.persistence.backends.sqlite import SQLiteBackend
from workbench_agent.runtime.graphs.registry import foundation_worker_registry
from workbench_agent.runtime.models.invoker import ModelInvoker
from workbench_agent.tools.mcp import (
    MCPAdapter,
    StreamableSessionFactory,
    configured_connection_policy,
)
from workbench_agent.tools.permissions import DeploymentToolPermissions
from workbench_agent.tools.policy import PolicyToolExecutor
from workbench_agent.tools.registry import ToolRegistry
from workbench_agent.runtime.graphs.tool_conversation import build_tool_conversation


async def setup_tool_run(platform, client, monkeypatch, model):
    from support.mcp import mcp_server_source
    monkeypatch.syspath_prepend(str(mcp_server_source()))
    from workbench_mcp_server.bootstrap import Runtime
    from workbench_mcp_server.infrastructure.config.settings import Settings
    from workbench_mcp_server.interfaces.mcp.server import create_http_app

    monkeypatch.setenv('QA_TEST_AGENT', 'test-agent')
    monkeypatch.setenv('QA_TEST_UPSTREAM', 'test-upstream')
    mcp_scope = {
        'team': 'ExampleTeam',
        'environment': 'test',
        'region': 'id',
        'service': 'checkout',
        'source_id': 'kb',
    }
    settings = Settings.model_validate({
        'server': {'allowed_hosts': ['localhost:*']},
        'principals': [{
            'id': 'agent',
            'token_secret_ref': 'env://QA_TEST_AGENT',
            'tools': ['knowledge_search'],
            'scopes': [mcp_scope],
        }],
        'sources': [{
            'id': 'kb',
            'kind': 'knowledge',
            'adapter': 'normalized-http-v1',
            'endpoint': 'https://example.invalid/query',
            'auth_secret_ref': 'env://QA_TEST_UPSTREAM',
            'binding_id': 'fixture-kb',
            'scopes': [mcp_scope],
            'scope_enforced': True,
        }],
    })
    runtime = Runtime(settings)
    calls = []
    class Gateway:
        async def query(self, request):
            calls.append(request)
            return {'matches': [{
                'source_id': 'guide-1',
                'title': 'Guide',
                'content': 'Check the queue before retrying.',
                'source_uri': None,
            }]}
    runtime.executor.gateways['kb'] = Gateway()
    app = create_http_app(runtime)
    schema = {
        'type': 'object',
        'properties': {
            'query': {'type': 'string'},
            'scene': {'type': 'string'},
            'scope': {
                'type': 'object',
                'properties': {
                    'team': {'type': 'string'},
                    'service': {'type': 'string'},
                    'environment': {'type': 'string'},
                    'region': {'type': 'string'},
                    'source_id': {'type': 'string'},
                },
            },
        },
        'required': ['query', 'scene', 'scope'],
    }
    class Adapter:
        async def call(self, *args, **kwargs):
            return SimpleNamespace(
                isError=False,
                structuredContent={
                    'schema_version': 'dod-tool-result-v1',
                    'status': 'ok',
                    'data': {'matches': [{'source_id': 'guide-1', 'title': 'Guide', 'content': 'Check the queue before retrying.'}]},
                    'meta': {'truncated': False},
                },
            )
        async def discover(self, *args, **kwargs):
            raise AssertionError('discovery is not part of this graph test')
    policy = None
    adapter = Adapter()
    catalog = SimpleNamespace(
        names=('knowledge_search',),
        tools=({'name': 'knowledge_search', 'inputSchema': schema},),
    )
    binding = ToolBinding(
        alias='knowledge.search',
        provider='mcp',
        tool_name='knowledge_search',
        server='local',
        credential_scope='read',
        permission='knowledge.read',
        input_schema=schema,
    )
    ref = await ToolRegistry(platform.backend).publish(
        platform.auth, platform.agent_id, binding, key='tool-use'
    )
    context = await platform.contexts.resolve(platform.auth, platform.agent_id)
    agent = await platform.profiles.get_agent(platform.auth, platform.agent_id)
    saved = await platform.profiles.save_draft(
        platform.auth,
        platform.agent_id,
        ProfileDraft(
            default_model=context.default_model,
            graph_id='conversation.tool_use',
            allowed_modes=('qa',),
            tools=(ref,),
        ),
        agent['revision'],
        key='tool-use-profile',
    )
    await platform.profiles.publish(
        platform.auth, platform.agent_id, saved['revision'], key='tool-use-profile-publish'
    )
    rules = {
        'users': {platform.auth.subject: ['knowledge.read']},
        'teams': {platform.auth.team_id: ['knowledge.read']},
        'agents': {platform.agent_id: ['knowledge.read']},
        'skills': {'__none__': ['knowledge.read']},
        'experts': {'knowledge': ['knowledge.read']},
    }
    permissions = DeploymentToolPermissions(lambda: rules)
    payload = make_run_payload(platform.agent_id)
    payload['input']['text'] = 'What should I do before retrying?'
    scope = dict(SCOPE, start_at='2026-11-13T00:00:00.000Z', end_at='2026-11-13T00:10:00.000Z')
    payload['business_context'] = {'conversation': True, 'query_scope': scope}
    response = await client.post(
        '/v1/runs', json=payload, headers={'Idempotency-Key': 'tool-use-run'}
    )
    assert response.status_code == 202, response.text
    run_id = response.json()['run_id']
    class Factory:
        def build(self, *args, **kwargs):
            return model
    backend = SQLiteBackend(
        platform.settings.storage.database_url.get_secret_value(),
        tool_permissions=permissions,
    )
    backend._mcp_connection_policy = policy
    backend._diagnosis_factory = lambda execution: (
        ModelInvoker(backend, Factory(), object(), execution=execution),
        PolicyToolExecutor(backend, adapter, execution=execution, delays=(0, 0)),
    )
    registry = foundation_worker_registry(
        backend, supported_graph_ids=['conversation.tool_use']
    )
    worker = Worker(backend, 'tool-worker', registry)
    return runtime, app, backend, worker, calls, run_id


class ToolModel:
    def bind_tools(self, tools):
        return self
    async def ainvoke(self, messages, **kwargs):
        usage = {'input_tokens': 8, 'output_tokens': 3, 'total_tokens': 11}
        if any(isinstance(message, ToolMessage) for message in messages):
            return AIMessage(
                content='Check the queue before retrying.',
                usage_metadata=usage,
            )
        return AIMessage(
            content='',
            tool_calls=[{
                'name': 'knowledge_search',
                'args': {'query': 'retry', 'scene': 'qa'},
                'id': 'call-1',
                'type': 'tool_call',
            }],
            usage_metadata=usage,
        )


@pytest.mark.asyncio
async def test_tool_conversation_runs_tool_and_projects_unverified_answer(
    platform, client, monkeypatch
):
    runtime, app, backend, worker, calls, run_id = await setup_tool_run(
        platform, client, monkeypatch, ToolModel()
    )
    try:
        outcome = await worker.run_once()
        if outcome != 'projected':
            from sqlalchemy import select
            from workbench_agent.persistence import schema
            async with backend.create_uow().begin_read() as uow:
                run = await backend.runs.one(
                    uow, 'runs', team_id=platform.auth.team_id, run_id=run_id
                )
            raise AssertionError((outcome, dict(run)))
        result = (await client.get('/v1/runs/' + run_id)).json()
        assert result['status'] in {'succeeded', 'partial'}
        assert result['outcome']['completed'] is (result['status'] == 'succeeded')
        assert result['outcome']['summary'].startswith('以下回答基于所列工具返回的结果，未经独立校验')
        if result['outcome']['evidence_refs']:
            assert calls
        else:
            assert result['status'] == 'partial'
            assert '工具证据' in ' '.join(result['outcome']['unresolved_items'])
    finally:
        await backend.close()
        await runtime.close()


class NoToolModel:
    def bind_tools(self, tools):
        return self
    async def ainvoke(self, messages, **kwargs):
        return AIMessage(
            content='No tool was used.',
            usage_metadata={'input_tokens': 4, 'output_tokens': 2, 'total_tokens': 6},
        )


@pytest.mark.asyncio
async def test_tool_conversation_without_tool_is_partial(platform, client, monkeypatch):
    runtime, app, backend, worker, calls, run_id = await setup_tool_run(
        platform, client, monkeypatch, NoToolModel()
    )
    try:
        assert await worker.run_once() == 'projected'
        result = (await client.get('/v1/runs/' + run_id)).json()
        assert result['status'] == 'partial'
        assert '本轮未调用工具' in result['outcome']['summary']
        assert calls == []
    finally:
        await backend.close()
        await runtime.close()

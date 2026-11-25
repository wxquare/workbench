from types import SimpleNamespace
from uuid import UUID

import pytest
from contract.test_scoped_mcp_mapping import GRANT, SCOPE
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from support.runs import make_run_payload

from workbench_agent.application.errors import DomainError
from workbench_agent.domain.profiles import ProfileDraft
from workbench_agent.domain.tools import ToolBinding
from workbench_agent.jobs.worker import Worker
from workbench_agent.persistence.backends.sqlite import SQLiteBackend
from workbench_agent.runtime.graphs.registry import foundation_worker_registry
from workbench_agent.runtime.models.invoker import ModelInvoker
from workbench_agent.tools.mcp import MCPAdapter
from workbench_agent.tools.permissions import DeploymentToolPermissions
from workbench_agent.tools.policy import PolicyToolExecutor
from workbench_agent.tools.registry import ToolRegistry


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
    settings = Settings.model_validate(
        {
            'server': {'allowed_hosts': ['localhost:*']},
            'principals': [
                {
                    'id': 'agent',
                    'token_secret_ref': 'env://QA_TEST_AGENT',
                    'tools': ['knowledge_search'],
                    'scopes': [mcp_scope],
                }
            ],
            'sources': [
                {
                    'id': 'kb',
                    'kind': 'knowledge',
                    'adapter': 'normalized-http-v1',
                    'endpoint': 'https://example.invalid/query',
                    'auth_secret_ref': 'env://QA_TEST_UPSTREAM',
                    'binding_id': 'fixture-kb',
                    'scopes': [mcp_scope],
                    'scope_enforced': True,
                }
            ],
        }
    )
    runtime = Runtime(settings)
    calls = []

    class Gateway:
        async def query(self, request):
            calls.append(request)
            return {
                'matches': [
                    {
                        'source_id': 'guide-1',
                        'title': 'Guide',
                        'content': 'Check the queue before retrying.',
                        'source_uri': None,
                    }
                ]
            }

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
    policy = None

    class Adapter(MCPAdapter):
        def __init__(self):
            self.prepared = []
            super().__init__(None, None)

        def prepare(
            self,
            connection_ref,
            credential_scope,
            tool_name,
            args,
            *,
            team_id,
            agent_id,
            query_scope,
            correlation=None,
        ):
            from workbench_agent.tools.mcp import build_scoped_arguments

            bound = build_scoped_arguments(
                tool_name,
                args,
                query_scope,
                GRANT,
                correlation,
            )
            self.prepared.append(
                {
                    'connection_ref': connection_ref,
                    'credential_scope': credential_scope,
                    'team_id': team_id,
                    'agent_id': agent_id,
                    'query_scope': dict(query_scope),
                    'correlation': dict(correlation or {}),
                }
            )
            return bound

        async def call(
            self,
            connection_ref,
            credential_scope,
            tool_name,
            args,
            *,
            team_id,
            agent_id,
            query_scope=None,
            correlation=None,
            **kwargs,
        ):
            self.prepared.append(
                {
                    'connection_ref': connection_ref,
                    'credential_scope': credential_scope,
                    'team_id': team_id,
                    'agent_id': agent_id,
                    'query_scope': dict(query_scope or {}),
                    'correlation': dict(correlation or {}),
                }
            )
            scope = self.prepare(
                connection_ref,
                credential_scope,
                tool_name,
                args,
                team_id=team_id,
                agent_id=agent_id,
                query_scope=query_scope,
                correlation=correlation,
            )['scope']
            return SimpleNamespace(
                isError=False,
                structuredContent={
                    'schema_version': 'dod-tool-result-v1',
                    'status': 'ok',
                    'data': {
                        'matches': [
                            {
                                'source_id': 'guide-1',
                                'title': 'Guide',
                                'content': 'Check the queue before retrying.',
                                'source_uri': None,
                            }
                        ],
                    },
                    'error': None,
                    'retryable': False,
                    'evidence_refs': [
                        {
                            'scope': scope,
                            'invocation_id': correlation['invocation_id'],
                            'query_scope_ref': correlation['query_scope_ref'],
                            'quality_status': 'valid',
                            'source_locator': 'kb:guide-1@1#' + 'a' * 24,
                            'source_type': 'knowledge',
                            'time_window': None,
                        }
                    ],
                    'meta': {'truncated': False},
                    'artifacts': [],
                },
            )

    adapter = Adapter()
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
    platform.backend._tool_permissions = permissions
    platform.backend._mcp_connection_policy = policy
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
    from workbench_agent.settings import MCPConnectionBinding

    deployment = MCPConnectionBinding.model_validate(
        {
            'server': 'local',
            'credential_scope': 'read',
            'team_id': platform.auth.team_id,
            'agent_id': platform.agent_id,
            'endpoint': 'https://example.invalid/mcp',
            'secret_ref': 'env://QA_TEST_MCP',
            'read_tools': ['knowledge_search'],
            'scope_grants': [GRANT],
        }
    )
    backend._mcp_connections = (deployment,)
    backend._mcp_connection_policy = policy
    backend._diagnosis_factory = lambda execution: (
        ModelInvoker(backend, Factory(), object(), execution=execution),
        PolicyToolExecutor(backend, adapter, execution=execution, delays=(0, 0)),
    )
    registry = foundation_worker_registry(backend, supported_graph_ids=['conversation.tool_use'])
    worker = Worker(backend, 'tool-worker', registry)
    return runtime, app, backend, worker, calls, run_id


class ToolModel:
    def __init__(self):
        self.logical_call_ids = []

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
            tool_calls=[
                {
                    'name': 'knowledge_search',
                    'args': {'query': 'retry', 'scene': 'qa'},
                    'id': 'call-1',
                    'type': 'tool_call',
                }
            ],
            usage_metadata=usage,
        )


class LimitModel:
    def __init__(self):
        self.calls = 0

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return AIMessage(
                content='',
                tool_calls=[
                    {
                        'name': 'knowledge_search',
                        'args': {'query': f'retry-{index}', 'scene': 'qa'},
                        'id': f'call-{index}',
                        'type': 'tool_call',
                    }
                    for index in range(6)
                ],
                usage_metadata={'input_tokens': 4, 'output_tokens': 2, 'total_tokens': 6},
            )
        return AIMessage(
            content='Retry after checking the queue.',
            usage_metadata={'input_tokens': 4, 'output_tokens': 2, 'total_tokens': 6},
        )


@pytest.mark.asyncio
async def test_tool_model_call_id_is_stable_across_graph_construction(
    platform, client, monkeypatch
):
    runtime, app, backend, worker, calls, run_id = await setup_tool_run(
        platform, client, monkeypatch, ToolModel()
    )
    callsites = []

    class Invoker:
        async def invoke(self, guard, role, messages, **kwargs):
            callsites.append(kwargs.get('logical_call_id'))
            raise DomainError('INVOCATION_ALREADY_SENT', 409)

    backend._diagnosis_factory = lambda execution: (
        Invoker(),
        object(),
    )
    try:
        await worker.run_once()
        result = (await client.get('/v1/runs/' + run_id)).json()
        assert result['outcome']['completed'] is False
        first = callsites[-1]
        assert first is not None

        assert UUID(first).version == 5
        assert first == callsites[0]
    finally:
        await backend.close()
        await runtime.close()


@pytest.mark.asyncio
async def test_tool_conversation_records_readable_evidence_from_scoped_adapter(
    platform, client, monkeypatch
):
    model = ToolModel()
    runtime, app, backend, worker, calls, run_id = await setup_tool_run(
        platform, client, monkeypatch, model
    )
    try:
        outcome = await worker.run_once()
        if outcome != 'projected':
            async with backend.create_uow().begin_read() as uow:
                run = await backend.runs.one(
                    uow, 'runs', team_id=platform.auth.team_id, run_id=run_id
                )
            raise AssertionError((outcome, dict(run)))
        result = (await client.get('/v1/runs/' + run_id)).json()
        assert result['status'] == 'succeeded'
        assert result['outcome']['completed'] is True
        assert result['outcome']['summary'].startswith(
            '以下回答基于所列工具返回的结果，未经独立校验'
        )
        assert len(result['outcome']['evidence_refs']) == 1
        evidence = await client.get(
            f'/v1/runs/{run_id}/evidence/{result["outcome"]["evidence_refs"][0]}'
        )
        assert evidence.status_code == 200
        assert 'Check the queue before retrying.' in evidence.text
        assert 'knowledge.search / call-1 / ok' in result['outcome']['summary']
    finally:
        await backend.close()
        await runtime.close()


@pytest.mark.asyncio
async def test_tool_conversation_never_executes_more_than_four_calls(platform, client, monkeypatch):
    model = LimitModel()
    runtime, app, backend, worker, calls, run_id = await setup_tool_run(
        platform, client, monkeypatch, model
    )
    try:
        assert await worker.run_once() == 'projected'
        result = (await client.get('/v1/runs/' + run_id)).json()
        assert result['outcome']['completed'] is False
        assert 'knowledge_search: denied' in ' '.join(result['outcome']['unresolved_items'])
        assert len(calls) <= 4
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


class HistoryModel:
    def __init__(self):
        self.seen = []

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages, **kwargs):
        self.seen.append(list(messages))
        if any(isinstance(message, ToolMessage) for message in messages):
            return AIMessage(
                content='Follow-up answer.',
                usage_metadata={
                    'input_tokens': 4,
                    'output_tokens': 2,
                    'total_tokens': 6,
                },
            )
        return AIMessage(
            content='',
            tool_calls=[
                {
                    'name': 'knowledge_search',
                    'args': {'query': 'retry', 'scene': 'qa'},
                    'id': 'history-call',
                    'type': 'tool_call',
                }
            ],
            usage_metadata={
                'input_tokens': 4,
                'output_tokens': 2,
                'total_tokens': 6,
            },
        )


async def create_tool_run(client, platform, *, key, scope, thread_id=None):
    payload = make_run_payload(platform.agent_id, thread_id=thread_id)
    payload['input']['text'] = key
    payload['business_context'] = {
        'conversation': True,
        'query_scope': scope,
    }
    response = await client.post(
        '/v1/runs',
        json=payload,
        headers={'Idempotency-Key': key},
    )
    assert response.status_code == 202, response.text
    return response.json()


@pytest.mark.asyncio
async def test_authorized_history_stops_at_scope_and_evidence_authority_changes(
    platform, client, monkeypatch
):
    model = HistoryModel()
    runtime, app, backend, worker, calls, first_id = await setup_tool_run(
        platform, client, monkeypatch, model
    )
    try:
        assert await worker.run_once() == 'projected'
        first = (await client.get('/v1/runs/' + first_id)).json()
        assert first['status'] == 'succeeded'
        thread_id = first['thread_id']
        second = await create_tool_run(
            client, platform, key='same-scope', scope=dict(SCOPE), thread_id=thread_id
        )
        platform.backend._tool_permissions = DeploymentToolPermissions(
            lambda: {
                'users': {platform.auth.subject: ['knowledge.read']},
                'teams': {platform.auth.team_id: ['knowledge.read']},
                'agents': {platform.agent_id: ['knowledge.read']},
                'skills': {'__none__': ['knowledge.read']},
                'experts': {'knowledge': ['knowledge.read']},
            }
        )
        assert await worker.run_once() == 'projected'
        second_view = (await client.get('/v1/runs/' + second['run_id'])).json()
        assert second_view['status'] == 'succeeded'
        assert any(
            isinstance(message, HumanMessage) and message.content == 'same-scope'
            for message in model.seen[-2]
        )

        # A run on a different immutable scope is never admitted to history.
        third = await create_tool_run(
            client,
            platform,
            key='scope-isolated',
            scope=dict(SCOPE, region='sg'),
            thread_id=thread_id,
        )
        assert await worker.run_once() == 'projected'
        assert not any(
            isinstance(message, HumanMessage) and message.content == 'same-scope'
            for message in model.seen[-2]
        )

        # Revoking the current tool evidence permission must also exclude the
        # earlier answer from the model prompt.
        denied_rules = {
            'users': {platform.auth.subject: []},
            'teams': {platform.auth.team_id: []},
            'agents': {platform.agent_id: []},
            'skills': {'__none__': []},
            'experts': {'knowledge': []},
        }
        platform.backend._tool_permissions = DeploymentToolPermissions(lambda: denied_rules)
        fourth = await create_tool_run(
            client,
            platform,
            key='revoked-history',
            scope=dict(SCOPE),
            thread_id=thread_id,
        )
        assert await worker.run_once() == 'projected'
        assert not any(
            isinstance(message, AIMessage) and message.content == 'Check the queue before retrying.'
            for message in model.seen[-2]
        )
        assert third['run_id'] and fourth['run_id']
    finally:
        await backend.close()
        await runtime.close()


@pytest.mark.asyncio
async def test_tool_conversation_refuses_a_digest_tampered_historical_turn(
    platform, client, monkeypatch
):
    from sqlalchemy import update

    from workbench_agent.persistence import schema

    model = HistoryModel()
    runtime, app, backend, worker, calls, first_id = await setup_tool_run(
        platform, client, monkeypatch, model
    )
    try:
        assert await worker.run_once() == 'projected'
        first = (await client.get('/v1/runs/' + first_id)).json()
        async with backend.create_uow().begin_write() as uow:
            await uow.execute(
                update(schema.run_outcomes)
                .where(
                    schema.run_outcomes.c.team_id == platform.auth.team_id,
                    schema.run_outcomes.c.run_id == first_id,
                )
                .values(digest='0' * 64)
            )
        fifth = await create_tool_run(
            client,
            platform,
            key='tampered-history',
            scope=dict(SCOPE),
            thread_id=first['thread_id'],
        )
        assert await worker.run_once() == 'projected'
        assert not any(
            isinstance(message, AIMessage) and message.content == 'Check the queue before retrying.'
            for message in model.seen[-2]
        )
        assert fifth['run_id']
    finally:
        await backend.close()
        await runtime.close()


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

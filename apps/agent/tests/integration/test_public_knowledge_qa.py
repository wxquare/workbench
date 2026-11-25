import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from contract.test_scoped_mcp_mapping import GRANT, SCOPE
from workbench_agent.domain.profiles import ProfileDraft
from workbench_agent.domain.tools import ToolBinding
from workbench_agent.jobs.worker import Worker
from workbench_agent.persistence.backends.sqlite import SQLiteBackend
from workbench_agent.runtime.graphs.registry import foundation_worker_registry
from workbench_agent.runtime.models.invoker import ModelInvoker
from workbench_agent.tools.mcp import (
    ConnectionPolicy,
    MCPAdapter,
    StreamableSessionFactory,
    TrustedConnection,
)
from workbench_agent.tools.permissions import DeploymentToolPermissions
from workbench_agent.tools.policy import PolicyToolExecutor
from workbench_agent.tools.registry import ToolRegistry
from support.runs import make_run_payload


async def publish_qa(platform, tools=()):
    context = await platform.contexts.resolve(platform.auth, platform.agent_id)
    agent = await platform.profiles.get_agent(platform.auth, platform.agent_id)
    draft = await platform.profiles.save_draft(
        platform.auth,
        platform.agent_id,
        ProfileDraft(
            default_model=context.default_model, graph_id='knowledge.scoped_excerpt', tools=tools
        ),
        agent['revision'],
        key='qa-draft',
    )
    await platform.profiles.publish(
        platform.auth, platform.agent_id, draft['revision'], key='qa-publish'
    )


async def test_scope_admitted_atomically_and_idempotently(platform, client):
    await publish_qa(platform)
    payload = make_run_payload(platform.agent_id)
    payload['business_context'] = {'query_scope': SCOPE}
    response = await client.post('/v1/runs', json=payload, headers={'Idempotency-Key': 'qa'})
    assert response.status_code == 202
    run_id = response.json()['run_id']
    async with platform.backend.create_uow().begin_read() as uow:
        scope = await platform.backend.runs.current_scope(uow, platform.auth.team_id, run_id)
        assert json.loads(scope['scope_json']) == SCOPE
    replay = await client.post('/v1/runs', json=payload, headers={'Idempotency-Key': 'qa'})
    assert replay.json()['run_id'] == run_id
    assert await platform.count_rows('query_scope_revisions') == 1


async def test_scope_insert_failure_rolls_back_run(platform, client, monkeypatch):
    from workbench_agent.application.errors import DomainError

    await publish_qa(platform)

    async def reject(*args, **kwargs):
        raise DomainError('INVALID_SCOPE', 422)

    monkeypatch.setattr(client._transport.app.state.runs.contexts, 'query_scope', reject)
    payload = make_run_payload(platform.agent_id)
    payload['business_context'] = {'query_scope': SCOPE}
    response = await client.post('/v1/runs', json=payload, headers={'Idempotency-Key': 'rollback'})
    assert response.status_code == 422
    assert await platform.count_rows('runs') == 0
    assert await platform.count_rows('threads') == 0
    assert await platform.count_rows('run_jobs') == 0


async def test_cross_tenant_cannot_admit_or_read_scoped_run(platform, client):
    await publish_qa(platform)
    payload = make_run_payload(platform.agent_id)
    payload['business_context'] = {'query_scope': SCOPE}
    other = f'Bearer development:{platform.other_auth.team_id}:{platform.other_auth.subject}'
    denied = await client.post(
        '/v1/runs', json=payload, headers={'Authorization': other, 'Idempotency-Key': 'other'}
    )
    assert denied.status_code in (403, 404)
    response = await client.post('/v1/runs', json=payload, headers={'Idempotency-Key': 'owner'})
    assert response.status_code == 202
    denied = await client.get(
        '/v1/runs/' + response.json()['run_id'], headers={'Authorization': other}
    )
    assert denied.status_code == 404


async def test_qa_graph_rejects_other_modes(platform, client):
    await publish_qa(platform)
    payload = make_run_payload(platform.agent_id, mode='auto')
    response = await client.post(
        '/v1/runs', json=payload, headers={'Idempotency-Key': 'wrong-mode'}
    )
    assert response.status_code == 409
    assert await platform.count_rows('runs') == 0


async def test_bootstrap_preserves_explicit_qa_profile(platform):
    await publish_qa(platform)
    before = (await platform.profiles.get_agent(platform.auth, platform.agent_id))[
        'active_profile_id'
    ]
    await platform.bootstrap()
    after = (await platform.profiles.get_agent(platform.auth, platform.agent_id))[
        'active_profile_id'
    ]
    assert after == before


@pytest.mark.parametrize(
    'scope',
    [
        None,
        {},
        dict(SCOPE, team='other'),
        dict(SCOPE, source_id='other'),
        dict(SCOPE, region=' '),
        dict(SCOPE, end_at='bad'),
        dict(SCOPE, end_at=SCOPE['start_at']),
    ],
)
async def test_invalid_scope_rolls_back_admission(platform, client, scope):
    await publish_qa(platform)
    payload = make_run_payload(platform.agent_id)
    payload['business_context'] = {'query_scope': scope}
    response = await client.post('/v1/runs', json=payload, headers={'Idempotency-Key': 'bad-scope'})
    assert response.status_code == 422
    assert await platform.count_rows('runs') == 0
    assert await platform.count_rows('query_scope_revisions') == 0
    assert await platform.count_rows('run_jobs') == 0


@pytest.mark.parametrize(
    'scenario',
    [
        'answer',
        'fabricated',
        'unauthorized_scope',
        'revoked',
        'revoked_before',
        'revoked_during_summary',
        'recovery',
        'scheduler_recovery',
    ],
)
async def test_public_run_to_independent_worker(platform, client, monkeypatch, scenario):
    import httpx
    from support.mcp import mcp_server_source

    monkeypatch.syspath_prepend(str(mcp_server_source()))
    from workbench_mcp_server.bootstrap import Runtime
    from workbench_mcp_server.infrastructure.config.settings import Settings
    from workbench_mcp_server.interfaces.mcp.server import create_http_app

    monkeypatch.setenv('QA_TEST_AGENT', 'test-agent')
    monkeypatch.setenv('QA_TEST_UPSTREAM', 'test-upstream')
    scope = {
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
                    'scopes': [scope],
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
                    'scopes': [scope],
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
    try:
        async with app.app.router.lifespan_context(app.app):
            sessions = StreamableSessionFactory(
                {('local', 'read'): 'https://localhost:8000/mcp'},
                lambda *_: {'Authorization': 'Bearer test-agent'},
                transport=httpx.ASGITransport(app=app),
            )
            adapter = MCPAdapter(
                sessions,
                ConnectionPolicy(
                    [
                        TrustedConnection(
                            platform.auth.team_id,
                            platform.agent_id,
                            'local',
                            'read',
                            frozenset({'knowledge_search'}),
                            (GRANT,),
                        )
                    ]
                ),
            )
            catalog = await adapter.discover(
                'local', 'read', team_id=platform.auth.team_id, agent_id=platform.agent_id
            )
            schema = catalog.tools[catalog.names.index('knowledge_search')].inputSchema
            binding = ToolBinding(
                alias='kb.search',
                provider='mcp',
                tool_name='knowledge_search',
                server='local',
                credential_scope='read',
                permission='knowledge.read',
                input_schema=schema,
            )
            ref = await ToolRegistry(platform.backend).publish(
                platform.auth, platform.agent_id, binding, key='qa-tool'
            )
            await publish_qa(platform, (ref,))
            rules = {
                'users': {platform.auth.subject: ['knowledge.read']},
                'teams': {platform.auth.team_id: ['knowledge.read']},
                'agents': {platform.agent_id: ['knowledge.read']},
                'skills': {'__none__': ['knowledge.read']},
                'experts': {'knowledge': ['knowledge.read']},
            }
            permissions = DeploymentToolPermissions(lambda: rules)
            platform.backend._tool_permissions = permissions
            payload = make_run_payload(platform.agent_id)
            payload['input']['text'] = 'What should I do before retrying?'
            submitted_scope = dict(
                SCOPE, start_at='2026-11-13T00:00:00.000Z', end_at='2026-11-13T00:10:00.000Z'
            )
            if scenario == 'unauthorized_scope':
                submitted_scope['region'] = 'sg'
            payload['business_context'] = {'query_scope': submitted_scope}
            response = await client.post(
                '/v1/runs', json=payload, headers={'Idempotency-Key': 'qa-run'}
            )
            assert response.status_code == 202
            run_id = response.json()['run_id']

            class Model:
                def bind_tools(self, tools):
                    return self

                async def ainvoke(self, messages, **kwargs):
                    usage = {'input_tokens': 8, 'output_tokens': 3, 'total_tokens': 11}
                    if kwargs.get('response_format'):
                        data = json.loads(
                            next(m.content for m in messages if isinstance(m, HumanMessage))
                        )
                        ref = next(iter(data['sources']))
                        quote = (
                            'Invented instruction'
                            if scenario == 'fabricated'
                            else 'Check the queue before retrying.'
                        )
                        if scenario == 'revoked_during_summary':
                            rules['experts']['knowledge'] = []
                        return AIMessage(
                            content=json.dumps(
                                {'excerpts': [{'evidence_ref': ref, 'quote': quote}]}
                            ),
                            usage_metadata=usage,
                        )
                    if any(isinstance(m, ToolMessage) for m in messages):
                        return AIMessage(
                            content='Check the queue before retrying.', usage_metadata=usage
                        )
                    return AIMessage(
                        content='',
                        tool_calls=[
                            {
                                'name': 'kb_search',
                                'args': {'query': 'retry', 'scene': 'qa'},
                                'id': 'call-1',
                                'type': 'tool_call',
                            }
                        ],
                        usage_metadata=usage,
                    )

            class Factory:
                def build(self, *args, **kwargs):
                    return Model()

            worker_backend = SQLiteBackend(
                platform.settings.storage.database_url.get_secret_value(),
                tool_permissions=permissions,
            )
            try:
                if scenario == 'revoked_before':
                    rules['experts']['knowledge'] = []
                worker_backend._diagnosis_factory = lambda execution: (
                    ModelInvoker(worker_backend, Factory(), object(), execution=execution),
                    PolicyToolExecutor(worker_backend, adapter, execution=execution, delays=(0, 0)),
                )
                registry = foundation_worker_registry(worker_backend)
                worker = Worker(worker_backend, 'independent-worker', registry)
                if scenario == 'scheduler_recovery':
                    from workbench_agent.runtime.results import ResultProjector

                    original_project = ResultProjector.project

                    async def crash_projection(*args, **kwargs):
                        raise RuntimeError('crash after final checkpoint')

                    monkeypatch.setattr(ResultProjector, 'project', crash_projection)
                    assert await worker.run_once() == 'recovery_scheduled'
                    monkeypatch.setattr(ResultProjector, 'project', original_project)
                    from workbench_agent.cli.scheduler import run_scheduler

                    scheduler_settings = platform.settings.model_copy(
                        update={
                            'service': platform.settings.service.model_copy(
                                update={'tool_permissions': rules}
                            ),
                            'worker': platform.settings.worker.model_copy(
                                update={'supported_graph_ids': ['knowledge.scoped_excerpt']}
                            ),
                        }
                    )
                    report = await run_scheduler(scheduler_settings, once=True)
                    assert report['ready'] and report['recovered'] == 1
                if scenario == 'recovery':
                    original = PolicyToolExecutor.execute

                    async def crash_after(*args, **kwargs):
                        await original(*args, **kwargs)
                        raise RuntimeError('crash after tool receipt')

                    monkeypatch.setattr(PolicyToolExecutor, 'execute', crash_after)
                    assert await worker.run_once() == 'recovery_scheduled'
                    monkeypatch.setattr(PolicyToolExecutor, 'execute', original)
                    assert (await registry.reconciler.recover(run_id)).action == 'continue'
                if scenario != 'scheduler_recovery':
                    assert await worker.run_once() == 'projected'
                result = (await client.get('/v1/runs/' + run_id)).json()
                assert result['status'] == 'partial'
                if scenario in ('answer', 'revoked', 'recovery', 'scheduler_recovery'):
                    assert calls[0].scope.team == 'ExampleTeam' and calls[0].scope.source_id == 'kb'
                    assert len(calls) == 1
                    assert (
                        result['outcome']['findings'][0]['summary']
                        == 'Source excerpt: Check the queue before retrying.'
                    )
                    assert (
                        result['outcome']['findings'][0]['evidence_refs']
                        == result['outcome']['evidence_refs']
                    )
                    if scenario == 'revoked':
                        rules['experts']['knowledge'] = []
                        hidden = (await client.get('/v1/runs/' + run_id)).json()['outcome']
                        assert hidden['findings'] == [] and hidden['evidence_refs'] == []
                else:
                    assert result['outcome']['findings'] == []
                    if scenario in ('unauthorized_scope', 'revoked_before'):
                        assert calls == [] and result['outcome']['evidence_refs'] == []
                    if scenario == 'revoked_during_summary':
                        assert calls and result['outcome']['evidence_refs'] == []
            finally:
                await worker_backend.close()
    finally:
        await runtime.close()

import json
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from contract.test_dod_mcp_mapping import GRANT, SCOPE
from workbench_agent.domain.profiles import ProfileDraft
from workbench_agent.domain.tools import ToolBinding
from workbench_agent.jobs.worker import Worker
from workbench_agent.persistence.backends.sqlite import SQLiteBackend
from workbench_agent.runtime.graphs.registry import foundation_worker_registry
from workbench_agent.runtime.models.invoker import ModelInvoker
from workbench_agent.tools.mcp import ConnectionPolicy, MCPAdapter, StreamableSessionFactory, TrustedConnection
from workbench_agent.tools.permissions import DeploymentToolPermissions
from workbench_agent.tools.policy import PolicyToolExecutor
from workbench_agent.tools.registry import ToolRegistry
from support.runs import make_run_payload

@pytest.mark.parametrize('scenario', ['answer', 'fabricated', 'conflict', 'unanswered', 'revoked_during_summary', 'unanswerable', 'missing_age', 'stale', 'empty', 'revoked', 'revoked_before', 'unauthorized_scope', 'recovery', 'scheduler_recovery', 'summary_recovery', 'scheduler_expired'])
async def test_public_run_to_independent_worker(platform, client, monkeypatch, scenario):
    import httpx
    from support.mcp import mcp_server_source
    monkeypatch.syspath_prepend(str(mcp_server_source()))
    from workbench_mcp_server.bootstrap import Runtime
    from workbench_mcp_server.infrastructure.config.settings import Settings
    from workbench_mcp_server.interfaces.mcp.server import create_http_app

    monkeypatch.setenv('QA_TEST_AGENT', 'test-agent')
    monkeypatch.setenv('QA_TEST_UPSTREAM', 'test-upstream')
    scope = {'team': 'ExampleTeam', 'environment': 'test', 'region': 'id', 'service': 'checkout', 'source_id': 'kb'}
    settings = Settings.model_validate({'server': {'allowed_hosts': ['localhost:*']},
        'principals': [{'id': 'agent', 'token_secret_ref': 'env://QA_TEST_AGENT', 'tools': ['knowledge_search'], 'scopes': [scope]}],
        'sources': [{'id': 'kb', 'kind': 'knowledge', 'adapter': 'normalized-http-v1',
            'endpoint': 'https://example.invalid/query', 'auth_secret_ref': 'env://QA_TEST_UPSTREAM',
            'binding_id': 'fixture-kb', 'scopes': [scope], 'scope_enforced': True, 'allowed_source_hosts': ['docs.example.test']}]})
    runtime = Runtime(settings)
    calls, summaries = [], []
    class Gateway:
        async def query(self, request):
            calls.append(request)
            if scenario == 'empty': return {'matches': []}
            return {'matches': [{'source_id': 'guide-1', 'title': 'Guide',
                'content': 'Check the queue before retrying.', 'source_uri': 'https://docs.example.test/guide', 'updated_at': '2020-01-01T00:00:00Z' if scenario == 'stale' else '2026-11-18T00:00:00Z'}]}
    runtime.executor.gateways['kb'] = Gateway()
    app = create_http_app(runtime)
    try:
        async with app.app.router.lifespan_context(app.app):
            sessions = StreamableSessionFactory({('local', 'read'): 'https://localhost:8000/mcp'},
                lambda *_: {'Authorization': 'Bearer test-agent'}, transport=httpx.ASGITransport(app=app))
            adapter = MCPAdapter(sessions, ConnectionPolicy([TrustedConnection(platform.auth.team_id,
                platform.agent_id, 'local', 'read', frozenset({'knowledge_search'}), (GRANT,))]))
            catalog = await adapter.discover('local', 'read', team_id=platform.auth.team_id, agent_id=platform.agent_id)
            schema = catalog.tools[catalog.names.index('knowledge_search')].inputSchema
            binding = ToolBinding(alias='kb.search', provider='mcp', tool_name='knowledge_search',
                server='local', credential_scope='read', permission='knowledge.read', input_schema=schema)
            ref = await ToolRegistry(platform.backend).publish(platform.auth, platform.agent_id, binding, key='qa-tool')
            context = await platform.contexts.resolve(platform.auth, platform.agent_id)
            agent = await platform.profiles.get_agent(platform.auth, platform.agent_id)
            draft = await platform.profiles.save_draft(platform.auth, platform.agent_id,
                ProfileDraft(default_model=context.default_model, graph_version='dod.v19', tools=(ref,), knowledge_max_age_seconds=None if scenario == 'missing_age' else 31536000),
                agent['revision'], key='v19-draft')
            await platform.profiles.publish(platform.auth, platform.agent_id, draft['revision'], key='v19-publish')
            rules = {'users': {platform.auth.subject: ['knowledge.read']},
                'teams': {platform.auth.team_id: ['knowledge.read']},
                'agents': {platform.agent_id: ['knowledge.read']},
                'skills': {'__none__': ['knowledge.read']}, 'experts': {'knowledge': ['knowledge.read']}}
            permissions = DeploymentToolPermissions(lambda: rules)
            platform.backend._tool_permissions = permissions
            payload = make_run_payload(platform.agent_id)
            payload['input']['text'] = 'What should I do before retrying?'
            submitted_scope = dict(SCOPE, start_at='2026-11-13T00:00:00.000Z', end_at='2026-11-13T00:10:00.000Z')
            if scenario == 'unauthorized_scope':
                submitted_scope['region'] = 'sg'
            payload['business_context'] = {'query_scope': submitted_scope}
            response = await client.post('/v1/runs', json=payload, headers={'Idempotency-Key': 'qa-run'})
            assert response.status_code == 202
            run_id = response.json()['run_id']

            class Model:
                def bind_tools(self, tools):
                    return self
                async def ainvoke(self, messages, **kwargs):
                    usage = {'input_tokens': 8, 'output_tokens': 3, 'total_tokens': 11}
                    if kwargs.get('response_format'):
                        summaries.append(1)
                        data = json.loads(next(m.content for m in messages if isinstance(m, HumanMessage)))
                        ref = next(iter(data['sources']))
                        quote = 'Invented instruction' if scenario == 'fabricated' else 'Check the queue before retrying.'
                        if scenario == 'revoked_during_summary':
                            rules['experts']['knowledge'] = []
                        return AIMessage(content=json.dumps({'answerable': scenario != 'unanswerable', 'claims': [{'text': 'Check the queue before retrying.', 'evidence_ref': ref, 'source_id': 'guide-1', 'quote': quote}], 'unanswered': ['untrusted text'] if scenario == 'unanswered' else [], 'conflicts': ['untrusted text'] if scenario == 'conflict' else []}), usage_metadata=usage)
                    if any(isinstance(m, ToolMessage) for m in messages):
                        return AIMessage(content='Check the queue before retrying.', usage_metadata=usage)
                    return AIMessage(content='', tool_calls=[{'name': 'kb_search', 'args': {'query': 'retry', 'scene': 'qa'},
                        'id': 'call-1', 'type': 'tool_call'}], usage_metadata=usage)
            class Factory:
                def build(self, *args, **kwargs):
                    return Model()
            worker_backend = SQLiteBackend(platform.settings.storage.database_url.get_secret_value(), tool_permissions=permissions)
            try:
                if scenario == 'revoked_before':
                    rules['experts']['knowledge'] = []
                worker_backend._diagnosis_factory = lambda execution: (
                    ModelInvoker(worker_backend, Factory(), object(), execution=execution),
                    PolicyToolExecutor(worker_backend, adapter, execution=execution, delays=(0, 0)))
                registry = foundation_worker_registry(worker_backend)
                worker = Worker(worker_backend, 'independent-worker', registry)
                if scenario in ('scheduler_recovery', 'scheduler_expired'):
                    from workbench_agent.runtime.results import ResultProjector
                    original_project = ResultProjector.project
                    async def crash_projection(*args, **kwargs):
                        raise RuntimeError('crash after final checkpoint')
                    monkeypatch.setattr(ResultProjector, 'project', crash_projection)
                    assert await worker.run_once() == 'recovery_scheduled'
                    monkeypatch.setattr(ResultProjector, 'project', original_project)
                    if scenario == 'scheduler_expired':
                        from workbench_agent.runtime.graphs import evidence_authority
                        original_sanitize = evidence_authority.sanitize_v8_outcome
                        async def expire_at_projection(backend, uow, claim, outcome):
                            now = backend.clock.now_ms
                            calls_to_clock = 0
                            async def future(tx):
                                nonlocal calls_to_clock
                                calls_to_clock += 1
                                return await now(tx) + (400 * 86400 * 1000 if calls_to_clock > 1 else 0)
                            monkeypatch.setattr(backend.clock, 'now_ms', future)
                            try: return await original_sanitize(backend, uow, claim, outcome)
                            finally: monkeypatch.setattr(backend.clock, 'now_ms', now)
                        monkeypatch.setattr(evidence_authority, 'sanitize_v8_outcome', expire_at_projection)
                    from workbench_agent.cli.scheduler import run_scheduler
                    scheduler_settings = platform.settings.model_copy(update={'service':
                        platform.settings.service.model_copy(update={'tool_permissions': rules})})
                    report = await run_scheduler(scheduler_settings, once=True)
                    assert report['ready'] and report['recovered'] == 1
                if scenario == 'summary_recovery':
                    original_invoke = ModelInvoker.invoke
                    async def crash_after_summary(self, *args, **kwargs):
                        result = await original_invoke(self, *args, **kwargs)
                        if kwargs.get('output_schema') is not None:
                            raise RuntimeError('crash after model settlement before checkpoint')
                        return result
                    monkeypatch.setattr(ModelInvoker, 'invoke', crash_after_summary)
                    assert await worker.run_once() == 'recovery_scheduled'
                    monkeypatch.setattr(ModelInvoker, 'invoke', original_invoke)
                    assert (await registry.reconciler.recover(run_id)).action == 'continue'
                if scenario == 'recovery':
                    original = PolicyToolExecutor.execute
                    async def crash_after(*args, **kwargs):
                        await original(*args, **kwargs)
                        raise RuntimeError('crash after tool receipt')
                    monkeypatch.setattr(PolicyToolExecutor, 'execute', crash_after)
                    assert await worker.run_once() == 'recovery_scheduled'
                    monkeypatch.setattr(PolicyToolExecutor, 'execute', original)
                    assert (await registry.reconciler.recover(run_id)).action == 'continue'
                if scenario not in ('scheduler_recovery', 'scheduler_expired'):
                    assert await worker.run_once() == 'projected'
                result = (await client.get('/v1/runs/' + run_id)).json()
                assert result['status'] == ('succeeded' if scenario in ('answer', 'revoked', 'recovery', 'scheduler_recovery') else 'partial')
                if scenario in ('answer', 'revoked', 'recovery', 'scheduler_recovery'):
                    assert calls[0].scope.team == 'ExampleTeam' and calls[0].scope.source_id == 'kb'
                    assert len(calls) == 1
                    assert 'Check the queue before retrying.' in result['outcome']['findings'][0]['summary']
                    assert 'guide-1' in result['outcome']['findings'][0]['summary']
                    assert result['outcome']['findings'][0]['evidence_refs'] == result['outcome']['evidence_refs']
                    if scenario == 'revoked':
                        rules['experts']['knowledge'] = []
                        hidden = (await client.get('/v1/runs/' + run_id)).json()['outcome']
                        assert hidden['findings'] == [] and hidden['evidence_refs'] == []
                else:
                    if scenario not in ('conflict', 'unanswered'):
                        assert result['outcome']['findings'] == []
                    if scenario == 'empty': assert summaries == []
                    if scenario == 'summary_recovery': assert len(summaries) == 1
                    if scenario in ('conflict', 'unanswered'):
                        assert 'untrusted text' not in str(result['outcome']['unresolved_items'])
                        assert any(('conflicts' if scenario == 'conflict' else 'unanswered') in item for item in result['outcome']['unresolved_items'])
                    if scenario in ('unauthorized_scope', 'revoked_before'):
                        assert calls == [] and result['outcome']['evidence_refs'] == []
                    if scenario == 'revoked_during_summary':
                        assert calls and result['outcome']['evidence_refs'] == []
            finally:
                await worker_backend.close()
    finally:
        await runtime.close()

async def test_v19_rejects_missing_scope_and_other_modes(platform, client):
    context = await platform.contexts.resolve(platform.auth, platform.agent_id)
    agent = await platform.profiles.get_agent(platform.auth, platform.agent_id)
    draft = await platform.profiles.save_draft(platform.auth, platform.agent_id,
        ProfileDraft(default_model=context.default_model, graph_version='dod.v19'), agent['revision'], key='v19-scope-draft')
    await platform.profiles.publish(platform.auth, platform.agent_id, draft['revision'], key='v19-scope-publish')
    response = await client.post('/v1/runs', json=make_run_payload(platform.agent_id), headers={'Idempotency-Key':'missing-scope'})
    assert response.status_code == 422
    payload = make_run_payload(platform.agent_id, mode='auto')
    payload['business_context'] = {'query_scope': SCOPE}
    response = await client.post('/v1/runs', json=payload, headers={'Idempotency-Key':'wrong-mode'})
    assert response.status_code == 409
    assert await platform.count_rows('runs') == 0

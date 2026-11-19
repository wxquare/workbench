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
from workbench_agent.tools.mcp import configured_connection_policy, MCPAdapter, StreamableSessionFactory, TrustedConnection
from workbench_agent.tools.permissions import DeploymentToolPermissions
from workbench_agent.tools.policy import PolicyToolExecutor
from workbench_agent.tools.registry import ToolRegistry
from support.runs import make_run_payload


async def publish_qa(platform, tools=()):
    context = await platform.contexts.resolve(platform.auth, platform.agent_id)
    agent = await platform.profiles.get_agent(platform.auth, platform.agent_id)
    draft = await platform.profiles.save_draft(platform.auth, platform.agent_id,
        ProfileDraft(default_model=context.default_model, graph_version='dod.v19', tools=tools, knowledge_max_age_seconds=3153600000),
        agent['revision'], key='qa-draft')
    await platform.profiles.publish(platform.auth, platform.agent_id, draft['revision'], key='qa-publish')


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
    denied = await client.post('/v1/runs', json=payload, headers={'Authorization': other, 'Idempotency-Key': 'other'})
    assert denied.status_code in (403, 404)
    response = await client.post('/v1/runs', json=payload, headers={'Idempotency-Key': 'owner'})
    assert response.status_code == 202
    denied = await client.get('/v1/runs/' + response.json()['run_id'], headers={'Authorization': other})
    assert denied.status_code == 404


async def test_qa_graph_rejects_other_modes(platform, client):
    await publish_qa(platform)
    payload = make_run_payload(platform.agent_id, mode='auto')
    response = await client.post('/v1/runs', json=payload, headers={'Idempotency-Key': 'wrong-mode'})
    assert response.status_code == 409
    assert await platform.count_rows('runs') == 0


async def test_bootstrap_preserves_explicit_qa_profile(platform):
    await publish_qa(platform)
    before = (await platform.profiles.get_agent(platform.auth, platform.agent_id))['active_profile_id']
    await platform.bootstrap()
    after = (await platform.profiles.get_agent(platform.auth, platform.agent_id))['active_profile_id']
    assert after == before


@pytest.mark.parametrize('scope', [None, {}, dict(SCOPE, team='other'), dict(SCOPE, source_id='other'),
    dict(SCOPE, region=' '), dict(SCOPE, end_at='bad'), dict(SCOPE, end_at=SCOPE['start_at'])])
async def test_invalid_scope_rolls_back_admission(platform, client, scope):
    await publish_qa(platform)
    payload = make_run_payload(platform.agent_id)
    payload['business_context'] = {'query_scope': scope}
    response = await client.post('/v1/runs', json=payload, headers={'Idempotency-Key': 'bad-scope'})
    assert response.status_code == 422
    assert await platform.count_rows('runs') == 0
    assert await platform.count_rows('query_scope_revisions') == 0
    assert await platform.count_rows('run_jobs') == 0


@pytest.mark.parametrize('scenario', ['answer', 'fabricated', 'rejected', 'revoked', 'recovery', 'empty', 'unavailable', 'tamper', 'gap_recovery', 'model_recovery', 'unanswerable', 'budget', 'model_error', 'revoked_tool', 'revoked_checkpoint', 'changed_scope', 'grant_removed', 'grant_source', 'grant_scope', 'grant_removed_dispatch', 'grant_source_dispatch', 'grant_scope_dispatch', 'empty_source_swap', 'empty_source_recovery', 'legacy_empty_recovery', 'expired_source', 'missing_source_time'])
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
            'binding_id': 'fixture-kb', 'scopes': [scope], 'scope_enforced': True}]})
    runtime = Runtime(settings)
    calls = []
    class Gateway:
        async def query(self, request):
            calls.append(request)
            if scenario in ('empty', 'gap_recovery', 'empty_source_swap', 'empty_source_recovery', 'legacy_empty_recovery'): return {'matches': []}
            if scenario == 'unavailable': raise RuntimeError('unavailable')
            return {'matches': [{'source_id': 'guide-1', 'title': 'Guide',
                'content': 'Check the queue before retrying.', 'source_uri': None, 'updated_at': (None if scenario == 'missing_source_time' else '1900-01-01T00:00:00Z' if scenario == 'expired_source' else '2020-01-01T00:00:00Z')}]}
    runtime.executor.gateways['kb'] = Gateway()
    app = create_http_app(runtime)
    try:
        async with app.app.router.lifespan_context(app.app):
            sessions = StreamableSessionFactory({('local', 'read'): 'https://localhost:8000/mcp'},
                lambda *_: {'Authorization': 'Bearer test-agent'}, transport=httpx.ASGITransport(app=app))
            connection_rules = [TrustedConnection(platform.auth.team_id,
                platform.agent_id, 'local', 'read', frozenset({'knowledge_search'}), (GRANT,))]
            policy = configured_connection_policy(lambda: connection_rules)
            adapter = MCPAdapter(sessions, policy)
            platform.backend._mcp_connection_policy = policy
            def change_grant(kind):
                if kind == 'removed':
                    connection_rules.clear()
                else:
                    changed = dict(GRANT)
                    if kind == 'source': changed['sources'] = dict(GRANT['sources'], knowledge_search='other-kb')
                    if kind == 'scope': changed['allowed_scope'] = dict(GRANT['allowed_scope'], region='sg')
                    connection_rules[:] = [TrustedConnection(platform.auth.team_id, platform.agent_id,
                        'local', 'read', frozenset({'knowledge_search'}), (changed,))]
            catalog = await adapter.discover('local', 'read', team_id=platform.auth.team_id, agent_id=platform.agent_id)
            schema = catalog.tools[catalog.names.index('knowledge_search')].inputSchema
            binding = ToolBinding(alias='kb.search', provider='mcp', tool_name='knowledge_search',
                server='local', credential_scope='read', permission='knowledge.read', input_schema=schema)
            ref = await ToolRegistry(platform.backend).publish(platform.auth, platform.agent_id, binding, key='qa-tool')
            await publish_qa(platform, (ref,))
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
            if scenario == 'budget': payload['budgets'] = {'model_calls': 2}
            response = await client.post('/v1/runs', json=payload, headers={'Idempotency-Key': 'qa-run'})
            assert response.status_code == 202
            run_id = response.json()['run_id']

            model_roles = []
            delivered_tool_messages = []
            class Model:
                def bind_tools(self, tools):
                    return self
                async def ainvoke(self, messages, **kwargs):
                    usage = {'input_tokens': 8, 'output_tokens': 3, 'total_tokens': 11}
                    if kwargs.get('response_format'):
                        model_roles.append('structured')
                        data = json.loads(next(m.content for m in messages if isinstance(m, HumanMessage)))
                        if 'sources' in data:
                            source = data['sources'][0]
                            if scenario == 'model_error': return AIMessage(content='not JSON', usage_metadata=usage)
                            if scenario == 'unanswerable': return AIMessage(content=json.dumps({'answerable': False, 'missing': ['Missing relevant details']}), usage_metadata=usage)
                            quote = 'Invented instruction' if scenario == 'fabricated' else 'Check the queue before retrying.'
                            body = {'answerable': True, 'claims': [{'text': 'Check the queue before retrying.',
                                'citations': [{'evidence_ref': source['evidence_ref'], 'source_id': source['source_id'], 'quote': quote}]}]}
                        else:
                            body = {'checks': [{'index': 0, 'supported': scenario != 'rejected', 'reason': 'reviewed'}]}
                        return AIMessage(content=json.dumps(body), usage_metadata=usage)
                    if any(isinstance(m, ToolMessage) for m in messages):
                        delivered_tool_messages.extend(m.content for m in messages if isinstance(m, ToolMessage))
                        return AIMessage(content='Check the queue before retrying.', usage_metadata=usage)
                    return AIMessage(content='', tool_calls=[{'name': 'kb_search', 'args': {'query': 'retry', 'scene': 'qa'},
                        'id': 'call-1', 'type': 'tool_call'}], usage_metadata=usage)
            class Factory:
                def build(self, *args, **kwargs):
                    return Model()
            worker_backend = SQLiteBackend(platform.settings.storage.database_url.get_secret_value(), tool_permissions=permissions)
            try:
                worker_backend._mcp_connection_policy = policy
                if scenario == 'revoked_before':
                    rules['experts']['knowledge'] = []
                worker_backend._diagnosis_factory = lambda execution: (
                    ModelInvoker(worker_backend, Factory(), object(), execution=execution),
                    PolicyToolExecutor(worker_backend, adapter, execution=execution, delays=(0, 0)))
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
                    scheduler_settings = platform.settings.model_copy(update={'service':
                        platform.settings.service.model_copy(update={'tool_permissions': rules})})
                    report = await run_scheduler(scheduler_settings, once=True)
                    assert report['ready'] and report['recovered'] == 1
                if scenario == 'empty_source_swap':
                    original_execute = PolicyToolExecutor.execute
                    async def swap_after_empty(*args, **kwargs):
                        result = await original_execute(*args, **kwargs)
                        change_grant('source')
                        return result
                    monkeypatch.setattr(PolicyToolExecutor, 'execute', swap_after_empty)
                if scenario in ('empty_source_recovery', 'legacy_empty_recovery'):
                    original_execute = PolicyToolExecutor.execute
                    async def crash_after_empty(*args, **kwargs):
                        await original_execute(*args, **kwargs)
                        raise RuntimeError('crash after empty receipt')
                    monkeypatch.setattr(PolicyToolExecutor, 'execute', crash_after_empty)
                    assert await worker.run_once() == 'recovery_scheduled'
                    monkeypatch.setattr(PolicyToolExecutor, 'execute', original_execute)
                    if scenario == 'empty_source_recovery':
                        change_grant('source')
                    else:
                        from sqlalchemy import select, update
                        from workbench_agent.persistence import schema as db
                        async with worker_backend.create_uow().begin_write() as uow:
                            row = (await uow.execute(select(db.tool_invocations).where(db.tool_invocations.c.run_id == run_id, db.tool_invocations.c.status == 'succeeded'))).mappings().first()
                            receipt = json.loads(row['receipt_ref'])
                            receipt.pop('knowledge_scope')
                            await uow.execute(update(db.tool_invocations).where(db.tool_invocations.c.invocation_id == row['invocation_id']).values(receipt_ref=json.dumps(receipt)))
                    assert (await registry.reconciler.recover(run_id)).action == 'continue'
                if scenario in ('grant_removed_dispatch', 'grant_source_dispatch', 'grant_scope_dispatch'):
                    original_execute = PolicyToolExecutor.execute
                    async def change_grant_after_tool(*args, **kwargs):
                        result = await original_execute(*args, **kwargs)
                        change_grant(scenario.split('_')[1])
                        return result
                    monkeypatch.setattr(PolicyToolExecutor, 'execute', change_grant_after_tool)
                if scenario == 'revoked_tool':
                    original_execute = PolicyToolExecutor.execute
                    async def revoke_after_tool(*args, **kwargs):
                        result = await original_execute(*args, **kwargs)
                        rules['experts']['knowledge'] = []
                        return result
                    monkeypatch.setattr(PolicyToolExecutor, 'execute', revoke_after_tool)
                if scenario == 'changed_scope':
                    original_execute = PolicyToolExecutor.execute
                    async def change_scope_after_tool(*args, **kwargs):
                        result = await original_execute(*args, **kwargs)
                        async with worker_backend.create_uow().begin_write() as uow:
                            await platform.contexts.query_scope(uow, platform.auth, run_id, dict(submitted_scope, region='sg'))
                        return result
                    monkeypatch.setattr(PolicyToolExecutor, 'execute', change_scope_after_tool)
                if scenario == 'revoked_checkpoint':
                    from workbench_agent.runtime.experts.knowledge import KnowledgeChatModel
                    original_generate = KnowledgeChatModel._agenerate
                    async def crash_before_next_model(self, messages, *args, **kwargs):
                        if any(isinstance(m, ToolMessage) for m in messages):
                            raise RuntimeError('crash after tool checkpoint')
                        return await original_generate(self, messages, *args, **kwargs)
                    monkeypatch.setattr(KnowledgeChatModel, '_agenerate', crash_before_next_model)
                    assert await worker.run_once() == 'recovery_scheduled'
                    monkeypatch.setattr(KnowledgeChatModel, '_agenerate', original_generate)
                    rules['experts']['knowledge'] = []
                    assert (await registry.reconciler.recover(run_id)).action == 'continue'
                if scenario == 'model_recovery':
                    original_invoke = ModelInvoker.invoke
                    async def crash_after_model(self, guard, role, *args, **kwargs):
                        value = await original_invoke(self, guard, role, *args, **kwargs)
                        if role == 'summary': raise RuntimeError('crash after model sent')
                        return value
                    monkeypatch.setattr(ModelInvoker, 'invoke', crash_after_model)
                    assert await worker.run_once() == 'recovery_scheduled'
                    monkeypatch.setattr(ModelInvoker, 'invoke', original_invoke)
                    assert (await registry.reconciler.recover(run_id)).action == 'continue'
                if scenario == 'gap_recovery':
                    from workbench_agent.knowledge.gaps import KnowledgeGaps
                    original_record = KnowledgeGaps.record
                    async def crash_after_record(*args, **kwargs):
                        await original_record(*args, **kwargs)
                        raise RuntimeError('crash after gap commit')
                    monkeypatch.setattr(KnowledgeGaps, 'record', crash_after_record)
                    assert await worker.run_once() == 'recovery_scheduled'
                    monkeypatch.setattr(KnowledgeGaps, 'record', original_record)
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
                if scenario != 'scheduler_recovery':
                    assert await worker.run_once() == 'projected'
                result = (await client.get('/v1/runs/' + run_id)).json()
                if scenario in ('answer', 'revoked', 'recovery', 'tamper', 'grant_removed', 'grant_source', 'grant_scope'):
                    assert result['status'] == 'succeeded', json.dumps(result, ensure_ascii=False)
                    assert result['outcome']['knowledge_answer']['review_kind'] == 'model_reviewed'
                    assert result['outcome']['knowledge_answer']['claims'][0]['citations'][0]['quote'] == 'Check the queue before retrying.'
                    assert len(calls) == 1
                    if scenario in ('grant_removed', 'grant_source', 'grant_scope'):
                        change_grant(scenario.split('_')[1])
                        hidden = (await client.get('/v1/runs/' + run_id)).json()['outcome']
                        assert hidden['knowledge_answer'] is None and hidden['evidence_refs'] == []
                    if scenario == 'tamper':
                        from sqlalchemy import select, update
                        from workbench_agent.persistence import schema as db
                        async with worker_backend.create_uow().begin_write() as uow:
                            row = (await uow.execute(select(db.evidence_payloads).where(db.evidence_payloads.c.run_id == run_id))).mappings().first()
                            data = json.loads(row['payload'])
                            data['evidence_refs'][0]['scope']['region'] = 'different-region'
                            await uow.execute(update(db.evidence_payloads).where(db.evidence_payloads.c.evidence_id == row['evidence_id']).values(payload=json.dumps(data)))
                        hidden = (await client.get('/v1/runs/' + run_id)).json()['outcome']
                        assert hidden['knowledge_answer'] is None and hidden['evidence_refs'] == []
                    if scenario == 'revoked':
                        rules['experts']['knowledge'] = []
                        hidden = (await client.get('/v1/runs/' + run_id)).json()['outcome']
                        assert hidden['knowledge_answer'] is None
                        assert hidden['evidence_refs'] == []
                else:
                    assert result['status'] == 'partial', result
                    assert not result['outcome'].get('knowledge_answer')
                    if scenario == 'model_recovery': assert model_roles == ['structured']
                    if scenario in ('empty_source_swap', 'empty_source_recovery', 'legacy_empty_recovery'):
                        from workbench_agent.knowledge.gaps import KnowledgeGaps
                        page = await KnowledgeGaps(platform.backend).list(platform.auth)
                        assert not [item for item in page['items'] if item['run_id'] == run_id]
                        assert len(calls) == 1
                    if scenario in ('revoked_tool', 'revoked_checkpoint', 'changed_scope', 'grant_removed_dispatch', 'grant_source_dispatch', 'grant_scope_dispatch'):
                        assert delivered_tool_messages == []
                        assert len(calls) == 1
                    if scenario in ('empty', 'unavailable', 'fabricated', 'gap_recovery', 'unanswerable', 'budget', 'model_error'):
                        async with worker_backend.create_uow().begin_read() as uow:
                            checkpoint = await worker_backend.runs.checkpoint(uow, platform.auth.team_id, run_id)
                        assert checkpoint['values'].get('gap_reason') == {'empty': 'kb.empty', 'gap_recovery': 'kb.empty', 'unavailable': None, 'fabricated': 'kb.invalid_citation', 'unanswerable': 'kb.unanswerable', 'budget': None, 'model_error': None}[scenario]
                        from workbench_agent.knowledge.gaps import KnowledgeGaps
                        page = await KnowledgeGaps(platform.backend).list(platform.auth)
                        matching = [item for item in page['items'] if item['run_id'] == run_id]
                        assert len(matching) == (0 if scenario in ('unavailable', 'budget', 'model_error') else 1)
            finally:
                await worker_backend.close()
    finally:
        await runtime.close()


def test_structured_answer_json_accepts_json_arrays_and_uuid():
    from uuid import uuid4
    from workbench_agent.domain.knowledge_answers import AnswerDraft
    from workbench_agent.runtime.models.json_output import validate_json_output
    data = {'answerable': True, 'claims': [{'text': 'Fact', 'citations': [
        {'evidence_ref': str(uuid4()), 'source_id': 'source', 'quote': 'Fact'}]}]}
    assert validate_json_output(json.dumps(data), AnswerDraft).answerable


def test_structured_answer_json_rejects_coerced_boolean():
    from workbench_agent.domain.knowledge_answers import AnswerDraft
    from workbench_agent.runtime.models.json_output import validate_json_output
    with pytest.raises(ValueError):
        validate_json_output(json.dumps({'answerable': 'false', 'missing': ['unknown']}), AnswerDraft)


async def test_create_in_uses_caller_transaction_and_rejects_connector(platform):
    from workbench_agent.application.runs import RunService
    from workbench_agent.application.errors import DomainError
    from workbench_agent.domain.runs import CreateRun
    service = RunService(platform.backend)
    request = CreateRun.model_validate(make_run_payload(platform.agent_id))
    connector = platform.auth.model_copy(update={'identity_kind': 'connector'})
    async with platform.backend.create_uow().begin_write() as uow:
        with pytest.raises(DomainError):
            await service.create_in(uow, connector, request, 'connector')
    assert await platform.count_rows('runs') == 0
    async with platform.backend.create_uow().begin_write() as uow:
        accepted = await service.create_in(uow, platform.auth, request, 'user')
    assert (await platform.get_run(accepted.run_id)).run_id == accepted.run_id


def test_expert_result_preserves_all_tool_statuses():
    from workbench_agent.runtime.experts.knowledge import ExpertResult
    result = ExpertResult('task', 'text', (), ('empty', 'unavailable'))
    assert result.tool_statuses == ('empty', 'unavailable')

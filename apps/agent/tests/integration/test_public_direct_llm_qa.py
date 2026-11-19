import pytest
from langchain_core.messages import AIMessage

from workbench_agent.domain.profiles import ProfileDraft
from workbench_agent.jobs.worker import Worker
from workbench_agent.persistence.backends.sqlite import SQLiteBackend
from workbench_agent.runtime.graphs.registry import foundation_worker_registry
from workbench_agent.runtime.models.invoker import ModelInvoker
from support.runs import make_run_payload


async def publish_direct(platform):
    context = await platform.contexts.resolve(platform.auth, platform.agent_id)
    agent = await platform.profiles.get_agent(platform.auth, platform.agent_id)
    draft = await platform.profiles.save_draft(platform.auth, platform.agent_id,
        ProfileDraft(default_model=context.default_model, graph_id='conversation.single_turn', allowed_modes=('qa',)),
        agent['revision'], key='direct-draft')
    await platform.profiles.publish(platform.auth, platform.agent_id, draft['revision'], key='direct-publish')


@pytest.mark.parametrize('failure', [False, True])
async def test_direct_question_to_independent_worker(platform, client, failure):
    await publish_direct(platform)
    payload = make_run_payload(platform.agent_id)
    payload['input']['text'] = 'What is Python?'
    payload['business_context'] = {}
    response = await client.post('/v1/runs', json=payload, headers={'Idempotency-Key': 'direct'})
    assert response.status_code == 202
    run_id = response.json()['run_id']
    replay = await client.post('/v1/runs', json=payload, headers={'Idempotency-Key': 'direct'})
    assert replay.json()['run_id'] == run_id
    calls = []

    class Model:
        async def ainvoke(self, messages, **kwargs):
            calls.append(messages)
            if failure:
                raise RuntimeError('provider failure')
            return AIMessage(content='Python is a programming language.',
                usage_metadata={'input_tokens': 8, 'output_tokens': 5, 'total_tokens': 13})

    class Factory:
        def build(self, *args, **kwargs):
            return Model()

    backend = SQLiteBackend(platform.settings.storage.database_url.get_secret_value())
    try:
        backend._code_factory = lambda execution: ModelInvoker(backend, Factory(), object(), execution=execution)
        worker = Worker(backend, 'direct-worker', foundation_worker_registry(backend))
        assert await worker.run_once() == 'projected'
        result = (await client.get('/v1/runs/' + run_id)).json()
        assert result['status'] == ('partial' if failure else 'succeeded')
        assert len(calls) == 1
        assert result['outcome']['evidence_refs'] == []
        assert result['outcome']['findings'] == []
        if failure:
            assert 'Python is a programming language.' not in result['outcome']['summary']
            assert result['outcome']['unresolved_items']
        else:
            assert 'Python is a programming language.' in result['outcome']['summary']
            assert '未经外部知识源验证' in result['outcome']['summary']
            assert result['outcome']['usage_summary']['model_calls'] == 1
        other = f'Bearer development:{platform.other_auth.team_id}:{platform.other_auth.subject}'
        assert (await client.get('/v1/runs/' + run_id, headers={'Authorization': other})).status_code == 404
    finally:
        await backend.close()


async def test_direct_graph_rejects_other_modes(platform, client):
    await publish_direct(platform)
    payload = make_run_payload(platform.agent_id, mode='auto')
    response = await client.post('/v1/runs', json=payload, headers={'Idempotency-Key': 'wrong-mode'})
    assert response.status_code == 409


async def test_direct_graph_rejects_knowledge_scope(platform, client):
    await publish_direct(platform)
    payload = make_run_payload(platform.agent_id)
    payload['business_context'] = {'query_scope': {'service': 'checkout'}}
    response = await client.post('/v1/runs', json=payload, headers={'Idempotency-Key': 'scope'})
    assert response.status_code == 409


async def test_model_response_checkpoint_gap_does_not_resend(platform, client, monkeypatch):
    from workbench_agent.runtime.budgets.service import BudgetService

    await publish_direct(platform)
    response = await client.post('/v1/runs', json=make_run_payload(platform.agent_id),
        headers={'Idempotency-Key': 'checkpoint-gap'})
    run_id = response.json()['run_id']
    calls = []

    class Model:
        async def ainvoke(self, messages, **kwargs):
            calls.append(1)
            return AIMessage(content='Original response', usage_metadata={'input_tokens': 8, 'output_tokens': 5, 'total_tokens': 13})

    class Factory:
        def build(self, *args, **kwargs):
            return Model()

    backend = SQLiteBackend(platform.settings.storage.database_url.get_secret_value())
    try:
        backend._code_factory = lambda execution: ModelInvoker(backend, Factory(), object(), execution=execution)
        registry = foundation_worker_registry(backend)
        worker = Worker(backend, 'direct-worker', registry)
        original = BudgetService.partial_outcome
        async def crash(*args, **kwargs):
            raise RuntimeError('crash before checkpoint')
        monkeypatch.setattr(BudgetService, 'partial_outcome', crash)
        assert await worker.run_once() == 'recovery_scheduled'
        monkeypatch.setattr(BudgetService, 'partial_outcome', original)
        assert (await registry.reconciler.recover(run_id)).action == 'continue'
        assert await worker.run_once() == 'projected'
        assert len(calls) == 1
        result = (await client.get('/v1/runs/' + run_id)).json()
        assert result['status'] == 'partial'
        assert 'INVOCATION_ALREADY_SENT' in result['outcome']['unresolved_items'][0]
    finally:
        await backend.close()

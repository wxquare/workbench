import json
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import update

from workbench_agent.domain.profiles import ProfileDraft
from workbench_agent.domain.outcomes import RunOutcome
from workbench_agent.jobs.worker import Worker
from workbench_agent.persistence import schema
from workbench_agent.persistence.backends.sqlite import SQLiteBackend
from workbench_agent.runtime.graphs.registry import foundation_worker_registry
from workbench_agent.runtime.models.invoker import ModelInvoker
from support.runs import make_run_payload


async def test_run_trace_survives_admission_replay_and_worker_execution(
    platform, client, conversation_worker
):
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from workbench_agent.observability import Telemetry
    from workbench_agent.settings import ObservabilitySettings

    exporter = InMemorySpanExporter()
    config = ObservabilitySettings.model_validate({'tracing': {'enabled': True}})
    api = Telemetry(config, 'api', 'test', processor=SimpleSpanProcessor(exporter))
    worker_telemetry = Telemetry(config, 'worker', 'test', processor=SimpleSpanProcessor(exporter))
    api_backend = client._transport.app.state.backend
    worker, model, _ = conversation_worker
    api_backend._telemetry = api
    worker.backend._telemetry = worker_telemetry
    try:
        await publish(platform, 'conversation.multi_turn')
        run, payload = await create(client, platform.agent_id, 'trace-replay', 'sensitive question')
        replay = await client.post(
            '/v1/runs', json=payload, headers={'Idempotency-Key': 'trace-replay'}
        )
        assert replay.json()['run_id'] == run['run_id']
        assert await worker.run_once() == 'projected'
        spans = exporter.get_finished_spans()
        accept = next(
            s
            for s in spans
            if s.name == 'run.accept'
            and s.context.trace_id
            == next(x for x in spans if x.name == 'job.execute').context.trace_id
        )
        job = next(s for s in spans if s.name == 'job.execute')
        model_span = next(s for s in spans if s.name == 'model.generate')
        assert job.parent.span_id == accept.context.span_id
        assert model_span.context.trace_id == job.context.trace_id
        assert model_span.attributes['langfuse.observation.type'] == 'generation'
        assert 'sensitive question' not in str([dict(s.attributes) for s in spans])
        assert len(model.calls) == 1
    finally:
        del api_backend._telemetry
        del worker.backend._telemetry
        api.shutdown()
        worker_telemetry.shutdown()


async def test_skill_and_redacted_model_content_share_worker_trace(
    platform, client, conversation_worker, tmp_path, monkeypatch
):
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from workbench_agent.observability import Telemetry
    from workbench_agent.settings import ObservabilitySettings
    from workbench_agent.skill_management.objects import LocalSkillObjects

    monkeypatch.setenv('WORKBENCH_TEST_SECRET', 'test-"secret\\value')
    exporter = InMemorySpanExporter()
    settings = ObservabilitySettings.model_validate(
        {'tracing': {'enabled': True}, 'langfuse': {'content_mode': 'redacted_content'}}
    )
    telemetry = Telemetry(settings, 'worker', 'test', processor=SimpleSpanProcessor(exporter))
    worker, model, _ = conversation_worker
    worker.backend._telemetry = telemetry
    client._transport.app.state.settings.storage.skill_object_root = tmp_path / 'skill-objects'
    worker.backend._skill_objects = LocalSkillObjects(tmp_path / 'skill-objects')
    try:
        created = await client.post(
            '/v1/skills',
            headers={'Idempotency-Key': str(uuid4())},
            json={
                'name': 'trace-focus',
                'description': 'Focus',
                'content': 'Prioritize cash flow.',
            },
        )
        ref = (
            await client.post(
                f'/v1/skills/{created.json()["skill_id"]}/publish',
                headers={'If-Match': '0', 'Idempotency-Key': str(uuid4())},
            )
        ).json()
        await publish(platform, 'conversation.multi_turn', skills=(ref,))
        run, _ = await create(client, platform.agent_id, 'trace-skill', 'test-"secret\\value')
        assert await worker.run_once() == 'projected'
        spans = exporter.get_finished_spans()
        skill = next(s for s in spans if s.name == 'skill.load')
        generation = next(s for s in spans if s.name == 'model.generate')
        assert skill.context.trace_id == generation.context.trace_id
        captured = generation.attributes['langfuse.observation.input']
        assert 'Prioritize cash flow.' in captured
        assert '[REDACTED]' in captured
        assert 'test-"secret\\value' not in captured
        assert (await client.get('/v1/runs/' + run['run_id'])).json()['status'] == 'succeeded'
        assert len(model.calls) == 1
    finally:
        del worker.backend._telemetry
        telemetry.shutdown()


async def test_managed_credentials_disable_content_export(platform, client, conversation_worker):
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from workbench_agent.observability import Telemetry
    from workbench_agent.settings import ObservabilitySettings

    exporter = InMemorySpanExporter()
    settings = ObservabilitySettings.model_validate(
        {'tracing': {'enabled': True}, 'langfuse': {'content_mode': 'redacted_content'}}
    )
    telemetry = Telemetry(settings, 'worker', 'test', processor=SimpleSpanProcessor(exporter))
    worker, model, _ = conversation_worker
    model.managed_secrets = object()
    worker.backend._telemetry = telemetry
    try:
        await publish(platform, 'conversation.multi_turn')
        await create(client, platform.agent_id, 'managed-trace', 'fictional-managed-key')
        assert await worker.run_once() == 'projected'
        generation = next(s for s in exporter.get_finished_spans() if s.name == 'model.generate')
        assert 'langfuse.observation.input' not in generation.attributes
        assert 'langfuse.observation.output' not in generation.attributes
        assert 'fictional-managed-key' not in str(generation.attributes)
    finally:
        del worker.backend._telemetry
        telemetry.shutdown()


async def test_export_failure_does_not_fail_question(
    platform, client, conversation_worker, monkeypatch
):
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.trace.export import SpanExportResult
    from workbench_agent.observability import Telemetry
    from workbench_agent.settings import ObservabilitySettings

    attempts = []
    monkeypatch.setattr(
        OTLPSpanExporter,
        'export',
        lambda self, spans: (attempts.append(len(spans)), SpanExportResult.FAILURE)[1],
    )
    monkeypatch.setenv('LANGFUSE_BASE_URL', 'https://langfuse.example.test')
    monkeypatch.setenv('LANGFUSE_PUBLIC_KEY', f'pk-test-{uuid4()}')
    monkeypatch.setenv('LANGFUSE_SECRET_KEY', 'sk-test-private-value')
    settings = ObservabilitySettings.model_validate(
        {'tracing': {'enabled': True}, 'langfuse': {'enabled': True}}
    )
    telemetry = Telemetry(settings, 'worker', 'test')
    worker, model, _ = conversation_worker
    worker.backend._telemetry = telemetry
    try:
        await publish(platform, 'conversation.multi_turn')
        run, _ = await create(client, platform.agent_id, 'trace-export-failure', 'Question')
        assert await worker.run_once() == 'projected'
        telemetry.provider.force_flush(timeout_millis=5000)
        assert attempts
        assert (await client.get('/v1/runs/' + run['run_id'])).json()['status'] == 'succeeded'
        assert len(model.calls) == 1
    finally:
        del worker.backend._telemetry
        telemetry.shutdown()


async def publish(platform, version, **config):
    context = await platform.contexts.resolve(platform.auth, platform.agent_id)
    agent = await platform.profiles.get_agent(platform.auth, platform.agent_id)
    draft = await platform.profiles.save_draft(
        platform.auth,
        platform.agent_id,
        ProfileDraft(
            default_model=context.default_model, graph_id=version, allowed_modes=('qa',), **config
        ),
        agent['revision'],
        key=f'draft-{version}',
    )
    await platform.profiles.publish(
        platform.auth, platform.agent_id, draft['revision'], key=f'publish-{version}'
    )


@pytest.mark.parametrize('version', ['conversation.single_turn', 'conversation.multi_turn'])
async def test_published_agent_instructions_are_pinned_to_run(
    platform, client, conversation_worker, version
):
    worker, model, _ = conversation_worker
    await publish(
        platform,
        version,
        description='A math tutor',
        system_prompt='Explain as a math tutor.',
        response_language='zh',
        response_format='markdown',
    )
    run, _ = await create(client, platform.agent_id, 'configured', 'What is two plus two?')
    agent = await platform.profiles.get_agent(platform.auth, platform.agent_id)
    assert agent['draft']['system_prompt'] == 'Explain as a math tutor.'
    draft = ProfileDraft.model_validate({**agent['draft'], 'system_prompt': 'Changed prompt.'})
    saved = await platform.profiles.save_draft(
        platform.auth, platform.agent_id, draft, agent['revision'], key='changed'
    )
    await platform.profiles.publish(
        platform.auth, platform.agent_id, saved['revision'], key='changed-publish'
    )
    assert await worker.run_once() == 'projected'
    system = model.calls[0][0][1]
    assert 'Explain as a math tutor.' in system
    assert 'Changed prompt.' not in system
    assert 'Chinese' in system
    assert 'Markdown' in system
    assert (await client.get('/v1/runs/' + run['run_id'])).json()['status'] == 'succeeded'


async def test_conversation_injects_pinned_shared_skill(
    platform, client, conversation_worker, tmp_path
):
    from workbench_agent.skill_management.objects import LocalSkillObjects

    worker, model, _ = conversation_worker
    client._transport.app.state.settings.storage.skill_object_root = tmp_path / 'skill-objects'
    worker.backend._skill_objects = LocalSkillObjects(tmp_path / 'skill-objects')
    headers = {'Idempotency-Key': str(uuid4())}
    created = await client.post(
        '/v1/skills',
        headers=headers,
        json={'name': 'focus', 'description': 'Focus', 'content': 'Prioritize cash flow.'},
    )
    skill = created.json()
    first = await client.post(
        f'/v1/skills/{skill["skill_id"]}/publish',
        headers={'If-Match': '0', 'Idempotency-Key': str(uuid4())},
    )
    ref = first.json()
    await publish(platform, 'conversation.multi_turn', skills=(ref,))
    run, _ = await create(client, platform.agent_id, 'skill-run', 'Analyse the company')
    await client.patch(
        f'/v1/skills/{skill["skill_id"]}',
        headers={'If-Match': '1', 'Idempotency-Key': str(uuid4())},
        json={'description': 'Focus', 'content': 'Prioritize debt.'},
    )
    await client.post(
        f'/v1/skills/{skill["skill_id"]}/publish',
        headers={'If-Match': '2', 'Idempotency-Key': str(uuid4())},
    )
    assert await worker.run_once() == 'projected'
    assert (await client.get('/v1/runs/' + run['run_id'])).json()['status'] == 'succeeded'
    assert 'Prioritize cash flow.' in model.calls[0][0][1]
    assert 'Prioritize debt.' not in model.calls[0][0][1]


async def test_missing_skill_object_does_not_invoke_model(
    platform, client, conversation_worker, tmp_path
):
    from workbench_agent.skill_management.objects import LocalSkillObjects

    worker, model, _ = conversation_worker
    client._transport.app.state.settings.storage.skill_object_root = tmp_path / 'skill-objects'
    worker.backend._skill_objects = LocalSkillObjects(tmp_path / 'skill-objects')
    created = await client.post(
        '/v1/skills',
        headers={'Idempotency-Key': str(uuid4())},
        json={'name': 'missing', 'description': 'Missing', 'content': 'Important instruction.'},
    )
    ref = (
        await client.post(
            f'/v1/skills/{created.json()["skill_id"]}/publish',
            headers={'If-Match': '0', 'Idempotency-Key': str(uuid4())},
        )
    ).json()
    await publish(platform, 'conversation.multi_turn', skills=(ref,))
    run, _ = await create(client, platform.agent_id, 'missing-skill', 'Question')
    for obj in (tmp_path / 'skill-objects').iterdir():
        obj.unlink()
    assert await worker.run_once() == 'projected'
    result = (await client.get('/v1/runs/' + run['run_id'])).json()
    assert result['status'] == 'partial'
    assert 'SKILL_UNAVAILABLE' in result['outcome']['unresolved_items'][0]
    assert not model.calls


@pytest.mark.parametrize('history_limit', [0, 1])
async def test_conversation_history_limit_is_applied(
    platform, client, conversation_worker, history_limit
):
    worker, model, _ = conversation_worker
    await publish(platform, 'conversation.multi_turn', conversation_history_turns=history_limit)
    first, _ = await create(client, platform.agent_id, 'h1', 'first')
    assert await worker.run_once() == 'projected'
    second, _ = await create(client, platform.agent_id, 'h2', 'second', first['thread_id'])
    assert await worker.run_once() == 'projected'
    await create(client, platform.agent_id, 'h3', 'third', second['thread_id'])
    assert await worker.run_once() == 'projected'
    history = model.calls[-1][1:-1]
    assert history == ([] if history_limit == 0 else [('human', 'second'), ('ai', 'answer-2')])


async def test_agent_configuration_http_save_validation_and_legacy_defaults(platform, client):
    url = f'/v1/agents/{platform.agent_id}'
    agent = (await client.get(url)).json()
    original = agent['draft']
    payload = {
        **original,
        'description': 'Support assistant',
        'system_prompt': 'Be concise.',
        'response_language': 'en',
        'response_format': 'json',
        'conversation_history_turns': 2,
    }
    response = await client.patch(
        url,
        json=payload,
        headers={'If-Match': str(agent['revision']), 'Idempotency-Key': 'agent-config'},
    )
    assert response.status_code == 200, response.text
    assert response.json()['draft']['system_prompt'] == 'Be concise.'
    assert (await client.get(url)).json()['draft']['conversation_history_turns'] == 2
    invalid = await client.patch(
        url,
        json={**payload, 'conversation_history_turns': 9},
        headers={'If-Match': str(response.json()['revision']), 'Idempotency-Key': 'invalid-config'},
    )
    assert invalid.status_code == 422
    legacy = ProfileDraft.model_validate(original)
    assert legacy.system_prompt == '' and legacy.conversation_history_turns == 8
    assert 'system_prompt' not in legacy.model_dump(mode='json')


async def create(client, agent, key, text, thread=None):
    payload = make_run_payload(agent, thread_id=thread)
    payload['input']['text'] = text
    response = await client.post('/v1/runs', json=payload, headers={'Idempotency-Key': key})
    assert response.status_code == 202, response.text
    return response.json(), payload


class ControlledModel:
    def __init__(self):
        self.calls = []
        self.fail = False

    def build(self, *args, **kwargs):
        return self

    async def ainvoke(self, messages, **kwargs):
        self.calls.append([(message.type, message.content) for message in messages])
        if self.fail:
            raise RuntimeError('controlled provider failure')
        return AIMessage(
            content=f'answer-{len(self.calls)}',
            usage_metadata={'input_tokens': 8, 'output_tokens': 5, 'total_tokens': 13},
        )


@pytest.fixture
async def conversation_worker(platform):
    backend = SQLiteBackend(platform.settings.storage.database_url.get_secret_value())
    model = ControlledModel()
    backend._code_factory = lambda execution: ModelInvoker(
        backend, model, object(), execution=execution
    )
    registry = foundation_worker_registry(backend)
    yield Worker(backend, 'conversation-worker', registry), model, registry
    await backend.close()


async def test_two_turn_history_is_scoped_and_admission_replay_does_not_call_model(
    platform, client, conversation_worker
):
    worker, model, registry = conversation_worker
    await publish(platform, 'conversation.multi_turn')
    first, _ = await create(client, platform.agent_id, 'first', 'first question')
    assert await worker.run_once() == 'projected'
    second, payload = await create(
        client, platform.agent_id, 'second', 'follow up', first['thread_id']
    )
    assert await worker.run_once() == 'projected'
    result = (await client.get('/v1/runs/' + second['run_id'])).json()
    assert result['status'] == 'succeeded'
    assert model.calls[1][1:] == [
        ('human', 'first question'),
        ('ai', 'answer-1'),
        ('human', 'follow up'),
    ]
    replay = await client.post('/v1/runs', json=payload, headers={'Idempotency-Key': 'second'})
    assert replay.json()['run_id'] == second['run_id']
    assert (await registry.reconciler.recover(second['run_id'])).action == 'terminal'
    assert len(model.calls) == 2
    other = f'Bearer development:{platform.other_auth.team_id}:{platform.other_auth.subject}'
    assert (
        await client.get('/v1/runs/' + second['run_id'], headers={'Authorization': other})
    ).status_code == 404


async def test_failed_and_other_thread_turns_are_not_history(platform, client, conversation_worker):
    worker, model, _ = conversation_worker
    await publish(platform, 'conversation.multi_turn')
    failed, _ = await create(client, platform.agent_id, 'failed', 'failed question')
    model.fail = True
    assert await worker.run_once() == 'projected'
    assert (await client.get('/v1/runs/' + failed['run_id'])).json()['status'] == 'partial'
    model.fail = False
    _, _ = await create(client, platform.agent_id, 'other', 'other thread secret')
    assert await worker.run_once() == 'projected'
    follow, _ = await create(
        client, platform.agent_id, 'follow', 'safe question', failed['thread_id']
    )
    assert await worker.run_once() == 'projected'
    assert model.calls[-1][1:] == [('human', 'safe question')]
    assert (await client.get('/v1/runs/' + follow['run_id'])).json()['status'] == 'succeeded'


async def test_model_checkpoint_gap_recovery_does_not_resend(
    platform, client, conversation_worker, monkeypatch
):
    from workbench_agent.runtime.budgets.service import BudgetService

    worker, model, registry = conversation_worker
    await publish(platform, 'conversation.multi_turn')
    run, _ = await create(client, platform.agent_id, 'gap', 'question')
    original = BudgetService.partial_outcome

    async def crash(*args, **kwargs):
        raise RuntimeError('crash before checkpoint')

    monkeypatch.setattr(BudgetService, 'partial_outcome', crash)
    assert await worker.run_once() == 'recovery_scheduled'
    monkeypatch.setattr(BudgetService, 'partial_outcome', original)
    assert (await registry.reconciler.recover(run['run_id'])).action == 'continue'
    assert await worker.run_once() == 'projected'
    assert len(model.calls) == 1
    result = (await client.get('/v1/runs/' + run['run_id'])).json()
    assert result['status'] == 'partial'
    assert 'INVOCATION_ALREADY_SENT' in result['outcome']['unresolved_items'][0]


async def test_turn_finished_after_admission_is_not_history(platform, client, conversation_worker):
    worker, model, _ = conversation_worker
    await publish(platform, 'conversation.multi_turn')
    first, _ = await create(client, platform.agent_id, 'pending-first', 'pending question')
    second, _ = await create(
        client, platform.agent_id, 'already-queued', 'queued question', first['thread_id']
    )
    assert await worker.run_once() == 'projected'
    assert await worker.run_once() == 'projected'
    assert model.calls[1][1:] == [('human', 'queued question')]
    assert (await client.get('/v1/runs/' + second['run_id'])).json()['status'] == 'succeeded'


async def test_previous_direct_qa_is_available_after_upgrade(platform, client, conversation_worker):
    worker, model, _ = conversation_worker
    await publish(platform, 'conversation.single_turn')
    first, _ = await create(client, platform.agent_id, 'legacy', 'legacy secret')
    assert await worker.run_once() == 'projected'
    await publish(platform, 'conversation.multi_turn')
    payload = make_run_payload(platform.agent_id, thread_id=first['thread_id'])
    payload['input']['text'] = 'new question'
    payload['business_context'] = {'conversation': True}
    response = await client.post('/v1/runs', json=payload, headers={'Idempotency-Key': 'new'})
    assert response.status_code == 202, response.text
    assert await worker.run_once() == 'projected'
    assert model.calls[-1][1:] == [
        ('human', 'legacy secret'),
        ('ai', 'answer-1'),
        ('human', 'new question'),
    ]


async def test_conversation_waits_for_previous_turn(platform, client, conversation_worker):
    worker, model, _ = conversation_worker
    await publish(platform, 'conversation.multi_turn')
    first, _ = await create(client, platform.agent_id, 'busy-first', 'first question')
    payload = make_run_payload(platform.agent_id, thread_id=first['thread_id'])
    payload['input']['text'] = 'follow up'
    payload['business_context'] = {'conversation': True}
    headers = {'Idempotency-Key': 'busy-followup'}
    rejected = await client.post('/v1/runs', json=payload, headers=headers)
    assert rejected.status_code == 409
    assert rejected.json()['code'] == 'THREAD_BUSY'
    assert await worker.run_once() == 'projected'
    accepted = await client.post('/v1/runs', json=payload, headers=headers)
    assert accepted.status_code == 202, accepted.text
    assert await worker.run_once() == 'projected'
    assert model.calls[-1][1:] == [
        ('human', 'first question'),
        ('ai', 'answer-1'),
        ('human', 'follow up'),
    ]


@pytest.mark.parametrize(
    'corruption', ['digest', 'answer_message', 'future', 'wrong_agent', 'wrong_team']
)
async def test_invalid_or_out_of_scope_prior_result_is_not_history(
    platform, client, conversation_worker, corruption
):
    worker, model, _ = conversation_worker
    await publish(platform, 'conversation.multi_turn')
    first, _ = await create(client, platform.agent_id, 'prior', 'private question')
    assert await worker.run_once() == 'projected'
    current, _ = await create(
        client, platform.agent_id, 'current', 'current question', first['thread_id']
    )
    async with platform.backend.create_uow().begin_write() as uow:
        if corruption == 'digest':
            await uow.execute(
                update(schema.run_outcomes)
                .where(schema.run_outcomes.c.run_id == first['run_id'])
                .values(digest='0' * 64)
            )
        elif corruption == 'answer_message':
            await uow.execute(
                update(schema.messages)
                .where(
                    schema.messages.c.run_id == first['run_id'],
                    schema.messages.c.role == 'assistant',
                )
                .values(content_ref='{}')
            )
        elif corruption == 'future':
            current_row = await platform.backend.runs.one(uow, 'runs', run_id=current['run_id'])
            await uow.execute(
                update(schema.run_outcomes)
                .where(schema.run_outcomes.c.run_id == first['run_id'])
                .values(created_at=current_row['created_at'])
            )
        else:
            row = await platform.backend.runs.one(uow, 'runs', run_id=first['run_id'])
            versions = json.loads(row['versions'])
            versions['agent_id' if corruption == 'wrong_agent' else 'team_id'] = (
                platform.other_auth.team_id if corruption == 'wrong_team' else 'wrong-agent'
            )
            await uow.execute(
                update(schema.runs)
                .where(schema.runs.c.run_id == first['run_id'])
                .values(versions=json.dumps(versions))
            )
    assert await worker.run_once() == 'projected'
    assert model.calls[-1][1:] == [('human', 'current question')]


@pytest.mark.parametrize('history_limit', [0, 8])
async def test_revocation_blocks_execution_and_replay(
    platform, client, conversation_worker, history_limit
):
    worker, model, _ = conversation_worker
    await publish(platform, 'conversation.multi_turn', conversation_history_turns=history_limit)
    run, payload = await create(client, platform.agent_id, 'revoked', 'private question')
    await platform.teams.set_member(
        platform.auth, platform.auth.subject, 'admin', 'revoked', expected_revision=0, key='revoke'
    )
    assert await worker.run_once() == 'capability_blocked'
    assert not model.calls
    replay = await client.post('/v1/runs', json=payload, headers={'Idempotency-Key': 'revoked'})
    assert replay.status_code == 404


async def test_history_is_bounded_to_recent_complete_turns(platform, client, conversation_worker):
    worker, model, _ = conversation_worker
    await publish(platform, 'conversation.multi_turn')
    thread = None
    for index in range(10):
        run, _ = await create(
            client, platform.agent_id, f'turn-{index}', f'question-{index}', thread
        )
        thread = run['thread_id']
        assert await worker.run_once() == 'projected'
    history = model.calls[-1][1:-1]
    assert len(history) == 16
    assert history[0] == ('human', 'question-1')
    assert history[-1] == ('ai', 'answer-9')


async def test_checkpointed_answer_reconciles_without_model_replay(
    platform, client, conversation_worker, monkeypatch
):
    worker, model, registry = conversation_worker
    await publish(platform, 'conversation.multi_turn')
    run, _ = await create(client, platform.agent_id, 'projection', 'question')
    original = worker._project

    async def crash(*args):
        raise RuntimeError('projection unavailable')

    monkeypatch.setattr(worker, '_project', crash)
    assert await worker.run_once() == 'recovery_scheduled'
    monkeypatch.setattr(worker, '_project', original)
    assert (await registry.reconciler.recover(run['run_id'])).action == 'finalized'
    assert len(model.calls) == 1
    result = (await client.get('/v1/runs/' + run['run_id'])).json()
    assert result['status'] == 'succeeded'
    assert 'answer-1' in result['outcome']['summary']


async def test_cross_team_and_agent_cannot_join_thread(platform, client, conversation_worker):
    worker, model, _ = conversation_worker
    await publish(platform, 'conversation.multi_turn')
    first, _ = await create(client, platform.agent_id, 'owner', 'owner secret')
    assert await worker.run_once() == 'projected'
    agent = await platform.profiles.create_agent(platform.auth, 'other', key='other-agent')
    payload = make_run_payload(agent['agent_id'], thread_id=first['thread_id'])
    response = await client.post(
        '/v1/runs', json=payload, headers={'Idempotency-Key': 'cross-agent'}
    )
    assert response.status_code == 404
    other_agent = await platform.profiles.get_agent(platform.other_auth)
    payload['agent_id'] = other_agent['agent_id']
    other = f'Bearer development:{platform.other_auth.team_id}:{platform.other_auth.subject}'
    response = await client.post(
        '/v1/runs', json=payload, headers={'Idempotency-Key': 'cross-team', 'Authorization': other}
    )
    assert response.status_code == 404
    assert len(model.calls) == 1


async def test_large_history_does_not_exceed_byte_limit(platform, client, conversation_worker):
    worker, model, _ = conversation_worker
    await publish(platform, 'conversation.multi_turn')
    first, _ = await create(client, platform.agent_id, 'large', 'x' * 17000)
    assert await worker.run_once() == 'projected'
    _, _ = await create(
        client, platform.agent_id, 'after-large', 'small question', first['thread_id']
    )
    assert await worker.run_once() == 'projected'
    assert model.calls[-1][1:] == [('human', 'small question')]


@pytest.mark.parametrize('output', ['   ', 'x' * 65501])
async def test_invalid_model_output_is_partial_and_not_reused(
    platform, client, conversation_worker, monkeypatch, output
):
    worker, model, _ = conversation_worker
    await publish(platform, 'conversation.multi_turn')
    first, _ = await create(client, platform.agent_id, 'invalid', 'invalid question')
    original = model.ainvoke

    async def invalid(messages, **kwargs):
        return AIMessage(
            content=output,
            usage_metadata={'input_tokens': 8, 'output_tokens': 5, 'total_tokens': 13},
        )

    monkeypatch.setattr(model, 'ainvoke', invalid)
    assert await worker.run_once() == 'projected'
    result = (await client.get('/v1/runs/' + first['run_id'])).json()
    assert result['status'] == 'partial'
    assert result['outcome']['unresolved_items']
    monkeypatch.setattr(model, 'ainvoke', original)
    _, _ = await create(
        client, platform.agent_id, 'after-invalid', 'next question', first['thread_id']
    )
    assert await worker.run_once() == 'projected'
    assert model.calls[-1][1:] == [('human', 'next question')]


async def test_evidence_bearing_result_is_never_history(platform, client, conversation_worker):
    worker, model, _ = conversation_worker
    await publish(platform, 'conversation.multi_turn')
    first, _ = await create(client, platform.agent_id, 'evidence', 'evidence question')
    assert await worker.run_once() == 'projected'
    async with platform.backend.create_uow().begin_write() as uow:
        stored = await platform.backend.results.get(uow, platform.auth.team_id, first['run_id'])
        payload = json.loads(stored['payload'])
        payload['evidence_refs'] = [str(uuid4())]
        payload['knowledge_gaps'] = ['private evidence']
        outcome = RunOutcome.model_validate(payload)
        await uow.execute(
            update(schema.run_outcomes)
            .where(schema.run_outcomes.c.run_id == first['run_id'])
            .values(payload=outcome.canonical(), digest=outcome.digest())
        )
        await uow.execute(
            update(schema.messages)
            .where(schema.messages.c.message_id == stored['message_id'])
            .values(content_ref=json.dumps({'outcome': outcome.model_dump(mode='json')}))
        )
    _, _ = await create(
        client, platform.agent_id, 'after-evidence', 'next question', first['thread_id']
    )
    assert await worker.run_once() == 'projected'
    assert model.calls[-1][1:] == [('human', 'next question')]


async def test_history_respects_frozen_model_context_budget(platform, client, conversation_worker):
    worker, model, _ = conversation_worker
    await publish(platform, 'conversation.multi_turn')
    first, _ = await create(client, platform.agent_id, 'history-sized', 'x' * 14000)
    assert await worker.run_once() == 'projected'
    question = 'y' * 46000
    _, _ = await create(client, platform.agent_id, 'context-sized', question, first['thread_id'])
    assert await worker.run_once() == 'projected'
    assert model.calls[-1][1:] == [('human', question)]


async def test_unavailable_model_configuration_returns_partial(
    platform, client, conversation_worker, monkeypatch
):
    from workbench_agent.application.errors import DomainError
    from workbench_agent.application.profiles import ProfileService

    worker, model, _ = conversation_worker
    await publish(platform, 'conversation.multi_turn')
    run, _ = await create(client, platform.agent_id, 'unavailable-model', 'question')

    async def unavailable(*args, **kwargs):
        raise DomainError('MODEL_PROFILE_UNAVAILABLE', 409)

    monkeypatch.setattr(ProfileService, 'model_version', unavailable)
    assert await worker.run_once() == 'projected'
    result = (await client.get('/v1/runs/' + run['run_id'])).json()
    assert result['status'] == 'partial'
    assert 'MODEL_PROFILE_UNAVAILABLE' in result['outcome']['unresolved_items'][0]
    assert not model.calls

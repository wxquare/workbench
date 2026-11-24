"""Admission contracts: real HTTP, SQLite transactions and durable replay."""

import asyncio
import json
from uuid import uuid4
import pytest
from sqlalchemy import select, func, update
from workbench_agent.persistence import schema
from workbench_agent.persistence.backends.sqlite import SQLiteBackend, SQLiteUnitOfWork
from workbench_agent.persistence.errors import StorageError
from workbench_agent.api.app import create_app
from httpx import AsyncClient, ASGITransport


async def count(platform, table):
    async with platform.backend.create_uow().begin_read() as uow:
        return (
            await uow.execute(select(func.count()).select_from(getattr(schema, table)))
        ).scalar_one()


async def post(client, body, key='r1'):
    return await client.post('/v1/runs', json=body, headers={'Idempotency-Key': key})


async def test_same_key_replays_original_response_after_publication(client, platform, run_payload):
    first = await post(client, run_payload)
    assert first.status_code == 202
    await platform.publish_model('new-model')
    replay = await post(client, run_payload)
    assert replay.status_code == 202 and replay.content == first.content
    assert replay.headers['Idempotency-Replayed'] == 'true'
    for table in ('runs', 'messages', 'run_jobs', 'run_events'):
        assert await count(platform, table) == 1
    run = await client.get(first.json()['links']['self'])
    assert run.status_code == 200 and run.json()['status'] == 'queued'
    async with platform.backend.create_uow().begin_read() as uow:
        row = (await uow.execute(select(schema.runs))).mappings().one()
    from workbench_agent.domain.context import RunContext

    context = RunContext.model_validate_json(row['versions'])
    assert context.agent_profile.version == '2'
    assert row['last_event_seq'] == 1 and row['revision'] == 1


async def test_different_payload_conflicts(client, run_payload):
    assert (await post(client, run_payload)).status_code == 202
    changed = {**run_payload, 'response_language': 'en'}
    response = await post(client, changed)
    assert response.status_code == 409 and response.json()['code'] == 'IDEMPOTENCY_CONFLICT'


@pytest.mark.parametrize('thread', [False, True])
async def test_source_dedup_new_key_does_not_create_thread_or_run(
    client, platform, run_payload, thread
):
    if thread:
        created = await client.post(
            '/v1/threads',
            json={'agent_id': platform.agent_id},
            headers={'Idempotency-Key': 'thread'},
        )
        assert created.status_code == 201
        run_payload['thread_id'] = created.json()['thread_id']
    results = await asyncio.gather(*(post(client, run_payload, f'key-{i}') for i in range(4)))
    assert [r.status_code for r in results] == [202] * 4
    assert len({r.json()['run_id'] for r in results}) == 1
    for table in ('threads', 'messages', 'runs', 'run_jobs', 'run_events', 'source_events'):
        assert await count(platform, table) == 1
    changed = {**run_payload, 'input': {'type': 'text', 'text': 'different'}}
    assert (await post(client, changed, 'changed')).status_code == 409


async def test_same_key_concurrent_admission(client, platform, run_payload):
    results = await asyncio.gather(*(post(client, run_payload) for _ in range(4)))
    assert [r.status_code for r in results] == [202] * 4
    assert len({r.json()['run_id'] for r in results}) == 1
    assert await count(platform, 'run_jobs') == 1


@pytest.mark.parametrize(
    'text,status', [('x' * 65536, 202), ('界' * 21845, 202), ('界' * 21846, 413)]
)
async def test_input_limit_is_utf8_text_bytes(client, run_payload, text, status):
    run_payload['input']['text'] = text
    assert (await post(client, run_payload)).status_code == status


@pytest.mark.parametrize('key', ['', 'x' * 129])
async def test_key_limits(client, run_payload, key):
    assert (await post(client, run_payload, key)).status_code == 422


async def test_authorization_precedes_replay(client, platform, run_payload):
    assert (await post(client, run_payload)).status_code == 202
    async with platform.backend.create_uow().begin_write() as uow:
        await platform.backend.configuration.change(
            uow,
            'team_members',
            {'team_id': platform.auth.team_id, 'subject': platform.auth.subject},
            {'status': 'inactive'},
        )
    assert (await post(client, run_payload)).status_code == 404


async def test_queue_preserves_active_thread_pointer_and_messages_paginate(
    client, platform, run_payload
):
    first = await post(client, run_payload)
    assert first.status_code == 202
    thread_id, run_id = first.json()['thread_id'], first.json()['run_id']
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(
            update(schema.threads)
            .where(schema.threads.c.thread_id == thread_id)
            .values(active_run_id=run_id)
        )
        await uow.execute(
            update(schema.runs).where(schema.runs.c.run_id == run_id).values(status='waiting')
        )
    for i in range(3):
        next_body = {**run_payload, 'thread_id': thread_id, 'client_message_id': f'next-{i}'}
        assert (await post(client, next_body, f'next-{i}')).status_code == 202
    thread = await client.get(f'/v1/threads/{thread_id}')
    assert thread.json()['active_run_id'] == run_id
    page = await client.get(f'/v1/threads/{thread_id}/messages?limit=2')
    assert len(page.json()['items']) == 2 and page.json()['next_cursor']
    page2 = await client.get(
        f'/v1/threads/{thread_id}/messages',
        params={'limit': 2, 'after': page.json()['next_cursor']},
    )
    assert len(page2.json()['items']) == 2
    assert {i['message_id'] for i in page.json()['items']}.isdisjoint(
        i['message_id'] for i in page2.json()['items']
    )
    assert (await client.get(f'/v1/threads/{thread_id}/messages?limit=101')).status_code == 422


@pytest.mark.parametrize(
    'body,code',
    [
        ({'mode': 'sop'}, 'WORKFLOW_UNAVAILABLE'),
        ({'budgets': {'model_calls': 9}}, 'BUDGET_EXCEEDED'),
        ({'budgets': {'tokens': 80001}}, 'BUDGET_EXCEEDED'),
    ],
)
async def test_mode_and_budget_restrictions(client, run_payload, body, code):
    response = await post(client, {**run_payload, **body})
    assert response.status_code in (409, 422) and response.json()['code'] == code


async def test_lower_budgets_are_persisted(client, platform, run_payload):
    result = await post(client, {**run_payload, 'budgets': {'model_calls': 2, 'tokens': 1000}})
    assert result.status_code == 202
    async with platform.backend.create_uow().begin_read() as uow:
        row = (await uow.execute(select(schema.runs))).mappings().one()
    assert json.loads(row['budgets']) == {
        'wall_seconds': 180,
        'model_calls': 2,
        'tool_calls': 20,
        'tokens': 1000,
    }


async def test_commit_failure_rolls_back_every_admission_row(
    client, platform, run_payload, monkeypatch
):
    async def fail(self):
        raise StorageError('STORAGE_UNAVAILABLE')

    with monkeypatch.context() as patch:
        patch.setattr(SQLiteUnitOfWork, 'commit', fail)
        response = await post(client, run_payload)
    assert response.status_code == 503
    for table in ('threads', 'messages', 'runs', 'run_jobs', 'run_events', 'source_events'):
        assert await count(platform, table) == 0
    assert (await post(client, run_payload)).status_code == 202


async def test_response_lost_after_commit_replays_from_fresh_backend(platform, run_payload):
    from workbench_agent.application.runs import RunService
    from workbench_agent.domain.runs import CreateRun

    first = await RunService(platform.backend).create(
        platform.auth, CreateRun.model_validate(run_payload), 'lost'
    )
    fresh = SQLiteBackend(platform.settings.storage.database_url.get_secret_value())
    try:
        replay = await RunService(fresh).create(
            platform.auth, CreateRun.model_validate(run_payload), 'lost'
        )
        assert replay.run_id == first.run_id and replay.replayed
    finally:
        await fresh.close()


async def test_expired_key_can_accept_new_input(client, platform, run_payload):
    first = await post(client, run_payload)
    assert first.status_code == 202
    async with platform.backend.create_uow().begin_write() as uow:
        receipt = await platform.backend.configuration.one(
            uow, 'idempotency_records', key='r1', route='run.create'
        )
        assert receipt['expires_at'] - receipt['updated_at'] == 259200000
        await platform.backend.configuration.change(
            uow, 'idempotency_records', {'record_id': receipt['record_id']}, {'expires_at': 1}
        )
    result = await post(client, {**run_payload, 'client_message_id': 'new-input'})
    assert result.status_code == 202 and result.json()['run_id'] != first.json()['run_id']


async def test_fixture_create_and_get_run(platform):
    accepted = await platform.create_run(key='fixture')
    view = await platform.get_run(accepted.run_id)
    assert view.status == 'queued' and view.context.agent_profile.version == '2'
    assert await platform.count_rows('runs') == 1


async def test_source_scope_is_actor_and_agent_scoped(client, platform, run_payload):
    first = await post(client, run_payload)
    assert first.status_code == 202
    # A second real member may use the same source ID without inheriting the first actor's run.
    async with platform.backend.create_uow().begin_write() as uow:
        await platform.backend.configuration.insert(
            uow,
            'team_members',
            dict(
                team_id=platform.auth.team_id,
                member_id=str(uuid4()),
                subject='operator-2',
                role='operator',
                status='active',
            ),
        )
    second = await client.post(
        '/v1/runs',
        json=run_payload,
        headers={
            'Idempotency-Key': 'r1',
            'Authorization': f'Bearer development:{platform.auth.team_id}:operator-2',
        },
    )
    assert second.status_code == 202 and second.json()['run_id'] != first.json()['run_id']
    agent = await platform.profiles.create_agent(platform.auth, 'second', key='second')
    from workbench_agent.domain.profiles import ProfileDraft

    draft = await platform.profiles.get_agent(platform.auth)
    saved = await platform.profiles.save_draft(
        platform.auth, agent['agent_id'], ProfileDraft.model_validate(draft['draft']), 0, key='save'
    )
    await platform.profiles.publish(
        platform.auth, agent['agent_id'], saved['revision'], key='publish'
    )
    third = await post(client, {**run_payload, 'agent_id': agent['agent_id']})
    assert third.status_code == 202 and third.json()['run_id'] != first.json()['run_id']
    assert await count(platform, 'runs') == 3


async def test_cross_team_resources_are_not_found(client, platform, run_payload):
    accepted = await post(client, run_payload)
    assert accepted.status_code == 202
    other = {'Authorization': f'Bearer development:{platform.other_auth.team_id}:other-admin'}
    for path in (
        accepted.json()['links']['self'],
        f'/v1/threads/{accepted.json()["thread_id"]}',
        f'/v1/threads/{accepted.json()["thread_id"]}/messages',
    ):
        assert (await client.get(path, headers=other)).status_code == 404
    assert (await client.get(f'/v1/runs/{uuid4()}')).status_code == 404


async def test_profile_policy_lowers_server_limits_and_restricts_modes(
    client, platform, run_payload
):
    from workbench_agent.domain.profiles import ProfileDraft

    agent = await platform.profiles.get_agent(platform.auth)
    policy = ProfileDraft.model_validate(
        {**agent['draft'], 'allowed_modes': [], 'budget_limits': {'model_calls': 3}}
    )
    saved = await platform.profiles.save_draft(
        platform.auth, platform.agent_id, policy, agent['revision'], key='policy'
    )
    await platform.profiles.publish(
        platform.auth, platform.agent_id, saved['revision'], key='publish-policy'
    )
    assert (await post(client, run_payload)).json()['code'] == 'MODE_NOT_ALLOWED'
    agent = await platform.profiles.get_agent(platform.auth)
    policy = policy.model_copy(update={'allowed_modes': ('qa',)})
    saved = await platform.profiles.save_draft(
        platform.auth, platform.agent_id, policy, agent['revision'], key='enable-qa'
    )
    await platform.profiles.publish(
        platform.auth, platform.agent_id, saved['revision'], key='publish-qa'
    )
    assert (await post(client, {**run_payload, 'budgets': {'model_calls': 4}})).json()[
        'code'
    ] == 'BUDGET_EXCEEDED'
    accepted = await post(client, run_payload)
    assert accepted.status_code == 202
    async with platform.backend.create_uow().begin_read() as uow:
        row = (await uow.execute(select(schema.runs))).mappings().one()
    assert json.loads(row['budgets'])['model_calls'] == 3


async def test_expected_revision_is_semantic_on_replay(client, run_payload):
    first = await post(client, run_payload)
    assert first.status_code == 202
    assert (await post(client, {**run_payload, 'expected_revision': 0})).json()[
        'code'
    ] == 'IDEMPOTENCY_CONFLICT'


async def test_text_limit_allows_json_escape_overhead(client, run_payload):
    run_payload['input']['text'] = '\n' * 65536
    assert (await post(client, run_payload)).status_code == 202


async def test_redis_unavailable_does_not_gate_durable_admission(
    platform, run_payload, monkeypatch
):
    # No Redis dependency is connected in Task4. A configured but unreachable server
    # must not be probed on the durable admission path (real fault transport is T09).
    monkeypatch.setenv('WORKBENCH_TEST_REDIS_URL', 'redis://127.0.0.1:1/0')
    configured = platform.settings.model_copy(
        update={
            'redis': platform.settings.redis.model_copy(
                update={'url_ref': 'env://WORKBENCH_TEST_REDIS_URL'}
            )
        }
    )
    app = create_app(configured, backend=platform.backend)
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url='http://test',
            headers={
                'Authorization': f'Bearer development:{platform.auth.team_id}:{platform.auth.subject}'
            },
        ) as client:
            assert (await post(client, run_payload)).status_code == 202
            replay = await post(client, run_payload)
            assert replay.status_code == 202 and replay.headers['Idempotency-Replayed'] == 'true'
    assert await count(platform, 'runs') == 1


async def test_message_pages_follow_creation_order_not_random_uuid(client, platform, run_payload):
    first = await post(client, run_payload)
    assert first.status_code == 202
    thread_id = first.json()['thread_id']
    second = await post(
        client, {**run_payload, 'thread_id': thread_id, 'client_message_id': 'second'}, 'second'
    )
    assert second.status_code == 202
    async with platform.backend.create_uow().begin_write() as uow:
        rows = list(
            (
                await uow.execute(select(schema.messages).order_by(schema.messages.c.message_id))
            ).mappings()
        )
        # Force creation order opposite lexical UUID order, independently of clock precision.
        await uow.execute(
            update(schema.messages)
            .where(schema.messages.c.message_id == rows[0]['message_id'])
            .values(created_at=2000)
        )
        await uow.execute(
            update(schema.messages)
            .where(schema.messages.c.message_id == rows[1]['message_id'])
            .values(created_at=1000)
        )
    page = await client.get(f'/v1/threads/{thread_id}/messages?limit=1')
    assert page.json()['items'][0]['message_id'] == rows[1]['message_id']
    page2 = await client.get(
        f'/v1/threads/{thread_id}/messages',
        params={'limit': 1, 'after': page.json()['next_cursor']},
    )
    assert page2.json()['items'][0]['message_id'] == rows[0]['message_id']


async def test_existing_published_profile_hash_survives_policy_extension(platform):
    from workbench_agent.application.idempotency import digest
    from workbench_agent.domain.profiles import ProfileDraft

    async with platform.backend.create_uow().begin_read() as uow:
        row = await platform.backend.configuration.one(
            uow, 'runtime_profiles', agent_id=platform.agent_id, status='published'
        )
    original = json.loads(row['profile_json'])
    assert set(original) == {
        'default_model',
        'role_models',
        'graph_id',
        'skills',
        'tools',
        'repositories',
        'allowed_modes',
    }
    assert original['allowed_modes'] == ['qa']
    restored = ProfileDraft.model_validate(original).model_dump(mode='json')
    assert digest(restored) == row['hash']


async def test_admission_job_is_durably_ready_for_spec_claimant(client, platform, run_payload):
    response = await post(client, run_payload)
    assert response.status_code == 202 and response.json()['status'] == 'queued'
    async with platform.backend.create_uow().begin_read() as uow:
        claimable = list(
            (
                await uow.execute(
                    select(schema.run_jobs).where(
                        schema.run_jobs.c.status.in_(('ready', 'retry_wait'))
                    )
                )
            ).mappings()
        )
        run = (await uow.execute(select(schema.runs))).mappings().one()
    assert len(claimable) == 1
    assert claimable[0]['run_id'] == response.json()['run_id']
    assert claimable[0]['status'] == 'ready' and claimable[0]['attempt'] == 0
    assert run['status'] == 'queued'


async def test_first_event_is_namespaced_admission_metadata_independent_of_http_receipt(
    client, platform, run_payload
):
    response = await post(client, run_payload)
    assert response.status_code == 202
    async with platform.backend.create_uow().begin_read() as uow:
        stored_event = (await uow.execute(select(schema.run_events))).mappings().one()
        run = (await uow.execute(select(schema.runs))).mappings().one()
    assert stored_event['type'] == 'run.accepted'
    assert stored_event['seq'] == 1 and stored_event['attempt'] == 0
    context = json.loads(run['versions'])
    assert json.loads(stored_event['payload']) == {
        'mode': 'qa',
        'phase': None,
        'attempt': 0,
        'versions': {
            'agent_profile': context['agent_profile'],
            'graph_id': 'conversation.multi_turn',
            'default_model': context['default_model'],
            'role_models': [],
            'skills': [],
            'tools': [],
            'repositories': [],
        },
    }
    assert set(response.json()) == {
        'run_id',
        'thread_id',
        'status',
        'revision',
        'accepted_at',
        'links',
    }
    replay = await post(client, run_payload)
    assert replay.status_code == 202 and replay.content == response.content
    assert replay.headers['Idempotency-Replayed'] == 'true'
    assert await count(platform, 'run_events') == 1

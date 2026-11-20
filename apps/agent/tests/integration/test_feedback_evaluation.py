"""Feedback is durable, idempotent, and protected by the run's tenant boundary."""
import pytest


@pytest.mark.asyncio
async def test_feedback_is_persisted_and_idempotent(client, platform):
    run = await platform.create_run()
    path = f'/v1/runs/{run.run_id}/feedback'
    headers = {'Idempotency-Key': 'feedback-one'}
    payload = {'rating': 1, 'comment': 'Clear evidence'}
    first = await client.post(path, headers=headers, json=payload)
    assert first.status_code == 201
    repeated = await client.post(path, headers=headers, json=payload)
    assert repeated.status_code == 201
    assert repeated.headers['idempotency-replayed'] == 'true'
    assert repeated.json() == first.json()
    conflict = await client.post(path, headers=headers, json={'rating': -1})
    assert conflict.status_code == 409
    assert await platform.count_rows('feedback') == 1
    assert (await platform.get_run(run.run_id)).status == run.status


@pytest.mark.asyncio
async def test_feedback_rejects_cross_tenant_and_bad_rating(client, platform):
    run = await platform.create_run()
    path = f'/v1/runs/{run.run_id}/feedback'
    other = {'Authorization': f'Bearer development:{platform.other_auth.team_id}:{platform.other_auth.subject}',
             'Idempotency-Key': 'feedback-other'}
    assert (await client.post(path, headers=other, json={'rating': 1})).status_code == 404
    assert (await client.post(path, headers={'Idempotency-Key': 'bad'}, json={'rating': 3})).status_code == 422
    assert await platform.count_rows('feedback') == 0


@pytest.mark.asyncio
async def test_feedback_list_is_agent_scoped_and_uses_pagination(client, platform):
    run = await platform.create_run()
    path = f'/v1/runs/{run.run_id}/feedback'
    for index in range(2):
        result = await client.post(path, headers={'Idempotency-Key': f'feedback-{index}'},
                                   json={'rating': 1 if index == 0 else -1, 'comment': f'comment-{index}'})
        assert result.status_code == 201
    first = await client.get(path, params={'limit': 1})
    assert first.status_code == 200
    assert len(first.json()['items']) == 1
    assert first.json()['next_cursor']
    second = await client.get(path, params={'limit': 1, 'after': first.json()['next_cursor']})
    assert len(second.json()['items']) == 1
    assert first.json()['items'][0]['feedback_id'] != second.json()['items'][0]['feedback_id']
    other = {'Authorization': f'Bearer development:{platform.other_auth.team_id}:{platform.other_auth.subject}'}
    assert (await client.get(path, headers=other)).status_code == 404


@pytest.mark.asyncio
async def test_negative_knowledge_feedback_creates_one_gap_atomically(client, platform):
    from integration.test_public_knowledge_qa import publish_qa
    from contract.test_dod_mcp_mapping import SCOPE
    from workbench_agent.persistence import schema
    from sqlalchemy import select, update
    context = await platform.contexts.resolve(platform.auth, platform.agent_id)
    from workbench_agent.domain.profiles import ProfileDraft
    agent = await platform.profiles.get_agent(platform.auth, platform.agent_id)
    draft = await platform.profiles.save_draft(
        platform.auth,
        platform.agent_id,
        ProfileDraft(default_model=context.default_model, graph_id='knowledge.citation_review'),
        agent['revision'],
        key='feedback-gap-profile',
    )
    await platform.profiles.publish(
        platform.auth,
        platform.agent_id,
        draft['revision'],
        key='feedback-gap-profile-publish',
    )
    payload = {
        'agent_id': platform.agent_id,
        'mode': 'qa',
        'input': {'type': 'text', 'text': 'Why is the answer stale?', 'artifact_ids': []},
        'business_context': {'query_scope': SCOPE},
    }
    run = await client.post(
        '/v1/runs',
        json=payload,
        headers={'Idempotency-Key': 'feedback-gap-run'},
    )
    assert run.status_code == 202, run.text
    run_id = run.json()['run_id']
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(
            update(schema.runs)
            .where(schema.runs.c.run_id == run_id)
            .values(status='partial')
        )
    path = f'/v1/runs/{run_id}/feedback'
    first = await client.post(
        path,
        headers={'Idempotency-Key': 'feedback-gap'},
        json={'rating': -1, 'comment': 'Too vague', 'reason': 'irrelevant'},
    )
    assert first.status_code == 201, first.text
    assert first.json()['knowledge_gap_id']
    replay = await client.post(
        path,
        headers={'Idempotency-Key': 'feedback-gap'},
        json={'rating': -1, 'comment': 'Too vague', 'reason': 'irrelevant'},
    )
    assert replay.json() == first.json()
    second = await client.post(
        path,
        headers={'Idempotency-Key': 'feedback-gap-two'},
        json={'rating': -1, 'comment': 'Still vague', 'reason': 'other'},
    )
    assert second.status_code == 201
    assert second.json()['knowledge_gap_id'] == first.json()['knowledge_gap_id']
    gaps = (await client.get('/v1/kb/gaps')).json()['items']
    assert [gap['gap_id'] for gap in gaps].count(first.json()['knowledge_gap_id']) == 1
    assert gaps[0]['problem_type'] == 'low_quality'


@pytest.mark.asyncio
async def test_positive_feedback_cannot_include_reason(client, platform):
    run = await platform.create_run()
    result = await client.post(
        f'/v1/runs/{run.run_id}/feedback',
        headers={'Idempotency-Key': 'positive-with-reason'},
        json={'rating': 1, 'reason': 'other'},
    )
    assert result.status_code == 422


@pytest.mark.asyncio
async def test_gap_insert_failure_rolls_back_feedback_and_receipt(
    client, platform, monkeypatch
):
    from integration.test_public_knowledge_qa import publish_qa
    from contract.test_dod_mcp_mapping import SCOPE
    from workbench_agent.persistence import schema
    from sqlalchemy import update
    context = await platform.contexts.resolve(platform.auth, platform.agent_id)
    from workbench_agent.domain.profiles import ProfileDraft
    agent = await platform.profiles.get_agent(platform.auth, platform.agent_id)
    draft = await platform.profiles.save_draft(
        platform.auth,
        platform.agent_id,
        ProfileDraft(default_model=context.default_model, graph_id='knowledge.citation_review'),
        agent['revision'],
        key='feedback-rollback-profile',
    )
    await platform.profiles.publish(
        platform.auth,
        platform.agent_id,
        draft['revision'],
        key='feedback-rollback-profile-publish',
    )
    payload = {
        'agent_id': platform.agent_id,
        'mode': 'qa',
        'input': {'type': 'text', 'text': 'Feedback rollback?', 'artifact_ids': []},
        'business_context': {'query_scope': SCOPE},
    }
    run = await client.post(
        '/v1/runs', json=payload, headers={'Idempotency-Key': 'feedback-rollback-run'}
    )
    run_id = run.json()['run_id']
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(
            update(schema.runs)
            .where(schema.runs.c.run_id == run_id)
            .values(status='partial')
        )
    async def fail(*args, **kwargs):
        raise RuntimeError('gap insert failed')
    monkeypatch.setattr(platform.backend.knowledge, 'insert_gap', fail)
    with pytest.raises(RuntimeError, match='gap insert failed'):
        await client.post(
            f'/v1/runs/{run_id}/feedback',
            headers={'Idempotency-Key': 'feedback-rollback'},
            json={'rating': -1, 'reason': 'other'},
        )
    assert await platform.count_rows('feedback') == 0
    assert await platform.count_rows('knowledge_gaps') == 0

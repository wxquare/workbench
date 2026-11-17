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

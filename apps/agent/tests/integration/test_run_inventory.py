from uuid import uuid4

from support.runs import make_run_payload


async def test_run_inventory_paginates_filters_and_isolates_agents(platform, client):
    ids = []
    for text in ('alpha question', 'beta question', 'gamma question'):
        payload = make_run_payload(platform.agent_id)
        payload['input']['text'] = text
        response = await client.post('/v1/runs', json=payload, headers={'Idempotency-Key': str(uuid4())})
        assert response.status_code == 202
        ids.append(response.json()['run_id'])

    path = '/v1/runs'
    params = {'agent_id': platform.agent_id, 'limit': 2}
    first = await client.get(path, params=params)
    assert first.status_code == 200
    page = first.json()
    assert len(page['items']) == 2
    assert page['next_cursor']
    second = await client.get(path, params={**params, 'after': page['next_cursor']})
    assert second.status_code == 200
    combined = page['items'] + second.json()['items']
    assert {item['run_id'] for item in combined} == set(ids)
    assert {item['title'] for item in combined} == {'alpha question', 'beta question', 'gamma question'}
    assert all('usage_summary' in item for item in combined)
    assert second.json()['next_cursor'] is None

    filtered = await client.get(path, params={'agent_id': platform.agent_id, 'q': 'beta', 'status': 'queued'})
    assert [item['run_id'] for item in filtered.json()['items']] == [ids[1]]
    assert (await client.get(path, params={'agent_id': platform.agent_id, 'status': 'succeeded'})).json()['items'] == []
    assert (await client.get(path, params={**params, 'after': str(uuid4())})).status_code == 422
    other = f'Bearer development:{platform.other_auth.team_id}:{platform.other_auth.subject}'
    assert (await client.get(path, params=params, headers={'Authorization': other})).status_code in (403, 404)

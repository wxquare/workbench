from uuid import uuid4

from support.runs import make_run_payload


async def test_thread_inventory_is_scoped_and_paginates(platform, client):
    ids = []
    for text in ('first', 'second', 'third'):
        payload = make_run_payload(platform.agent_id)
        payload['input']['text'] = text
        result = await client.post('/v1/runs', json=payload, headers={'Idempotency-Key': str(uuid4())})
        assert result.status_code == 202
        ids.append(result.json()['thread_id'])
    params = {'agent_id': platform.agent_id, 'limit': 2}
    first = await client.get('/v1/threads', params=params)
    assert first.status_code == 200
    page = first.json()
    assert len(page['items']) == 2 and page['next_cursor']
    second = await client.get('/v1/threads', params={**params, 'after': page['next_cursor']})
    assert second.status_code == 200
    assert {item['thread_id'] for item in page['items'] + second.json()['items']} == set(ids)
    assert second.json()['next_cursor'] is None
    assert (await client.get('/v1/threads', params={**params, 'after': str(uuid4())})).status_code == 422
    other = f'Bearer development:{platform.other_auth.team_id}:{platform.other_auth.subject}'
    assert (await client.get('/v1/threads', params=params, headers={'Authorization': other})).status_code in (403, 404)


async def test_conversation_request_rejects_record_only_graph(platform, client):
    payload = make_run_payload(platform.agent_id)
    payload['business_context'] = {'conversation': True}
    response = await client.post('/v1/runs', json=payload, headers={'Idempotency-Key': 'conversation'})
    assert response.status_code == 409


async def test_conversation_rejects_unsupported_completed_history(platform, client):
    from workbench_agent.domain.profiles import ProfileDraft
    from workbench_agent.jobs.worker import Worker
    from workbench_agent.runtime.graphs.registry import foundation_worker_registry

    response = await client.post('/v1/runs', json=make_run_payload(platform.agent_id),
        headers={'Idempotency-Key': 'legacy-record'})
    assert response.status_code == 202
    assert await Worker(platform.backend, 'inventory-worker', foundation_worker_registry(platform.backend)).run_once() == 'projected'
    context = await platform.contexts.resolve(platform.auth, platform.agent_id)
    agent = await platform.profiles.get_agent(platform.auth, platform.agent_id)
    draft = await platform.profiles.save_draft(platform.auth, platform.agent_id,
        ProfileDraft(default_model=context.default_model, graph_version='dod.v18', allowed_modes=('qa',)),
        agent['revision'], key='v18-draft')
    await platform.profiles.publish(platform.auth, platform.agent_id, draft['revision'], key='v18-publish')
    payload = make_run_payload(platform.agent_id, thread_id=response.json()['thread_id'])
    payload['business_context'] = {'conversation': True}
    result = await client.post('/v1/runs', json=payload, headers={'Idempotency-Key': 'followup'})
    assert result.status_code == 409
    assert result.json()['code'] == 'CONVERSATION_HISTORY_INCOMPATIBLE'

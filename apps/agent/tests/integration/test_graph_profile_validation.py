"""Unsupported graph IDs cannot be saved or published."""
import json
from uuid import uuid4
import pytest
from sqlalchemy import update
from workbench_agent.persistence import schema


@pytest.mark.parametrize('invalid', ['dod.v1', 'dod.v18', 'unknown.graph'])
async def test_invalid_graph_save_leaves_revision_unchanged(platform, client, invalid):
    url = '/v1/agents/' + platform.agent_id
    original = (await client.get(url)).json()
    response = await client.patch(url, json={**original['draft'], 'graph_id': invalid},
        headers={'If-Match': str(original['revision']), 'Idempotency-Key': str(uuid4())})
    assert response.status_code == 422
    assert response.json()['code'] == 'GRAPH_UNAVAILABLE'
    assert (await client.get(url)).json() == original


async def test_publish_rejects_preexisting_invalid_draft(platform, client):
    url = '/v1/agents/' + platform.agent_id
    original = (await client.get(url)).json()
    invalid = {**original['draft'], 'graph_id': 'unknown.graph'}
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(update(schema.runtime_profiles).where(schema.runtime_profiles.c.agent_id == platform.agent_id,
            schema.runtime_profiles.c.status == 'draft').values(profile_json=json.dumps(invalid), graph_id='unknown.graph'))
    response = await client.post(url + '/profiles/publish',
        headers={'If-Match': str(original['revision']), 'Idempotency-Key': str(uuid4())})
    assert response.status_code == 422
    assert response.json()['code'] == 'GRAPH_UNAVAILABLE'
    after = (await client.get(url)).json()
    assert after['revision'] == original['revision']
    assert after['active_profile_id'] == original['active_profile_id']


@pytest.mark.parametrize('graph_id', ['sop.conditional', 'rca.synthetic', 'rca.proposal', 'rca.batch_proposal',
    'knowledge.scoped_excerpt', 'conversation.single_turn', 'conversation.multi_turn', 'knowledge.citation_review'])
async def test_all_catalog_graphs_can_be_saved_and_published(platform, client, graph_id):
    url = '/v1/agents/' + platform.agent_id
    original = (await client.get(url)).json()
    headers = {'If-Match': str(original['revision']), 'Idempotency-Key': str(uuid4())}
    saved = await client.patch(url, json={**original['draft'], 'graph_id': graph_id}, headers=headers)
    assert saved.status_code == 200, saved.text
    published = await client.post(url + '/profiles/publish', headers={
        'If-Match': str(saved.json()['revision']), 'Idempotency-Key': str(uuid4())})
    assert published.status_code == 201, published.text
    assert published.json()['graph_id'] == graph_id


@pytest.mark.parametrize('graph_id', ['rca.synthetic', 'rca.proposal', 'rca.batch_proposal'])
async def test_rca_graphs_reject_ordinary_manual_runs(platform, client, graph_id):
    from integration.test_graph_catalog_api import publish_graph
    await publish_graph(platform, platform.auth, platform.agent_id, graph_id)
    response = await client.post('/v1/runs', json={
        'agent_id': platform.agent_id, 'mode': 'rca', 'input': {'text': 'ordinary request'}},
        headers={'Idempotency-Key': str(uuid4())})
    assert response.status_code == 409
    assert response.json()['code'] == 'WORKFLOW_UNAVAILABLE'
    assert await platform.count_rows('runs') == 0

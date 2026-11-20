"""Catalog routes require current team membership and count active team agents."""
from uuid import uuid4
from workbench_agent.domain.profiles import ProfileDraft


async def publish_graph(platform, auth, agent_id, graph_id):
    async with platform.backend.create_uow().begin_read() as uow:
        current = await platform.profiles.published(uow, auth, agent_id)
    agent = await platform.profiles.get_agent(auth, agent_id)
    saved = await platform.profiles.save_draft(auth, agent_id,
        ProfileDraft(default_model=current.default_model, graph_id=graph_id), agent['revision'], key=str(uuid4()))
    await platform.profiles.publish(auth, agent_id, saved['revision'], key=str(uuid4()))


async def test_catalog_is_public_to_team_members_and_describes_sop(platform, client):
    response = await client.get('/v1/graphs')
    assert response.status_code == 200
    assert len(response.json()['items']) == 9
    detail = await client.get('/v1/graphs/sop.conditional')
    assert detail.status_code == 200
    assert any(edge['source'] == edge['target'] == 'step' and edge['label'] for edge in detail.json()['edges'])
    for name in ('rca.synthetic', 'rca.proposal', 'rca.batch_proposal'):
        detail = await client.get('/v1/graphs/' + name)
        assert detail.json()['capabilities']['entrypoints'] == ['synthetic_alert']
    assert (await client.get('/v1/graphs/dod.v1')).status_code == 404
    assert (await client.get('/v1/graphs', headers={'Authorization': ''})).status_code == 401
    await platform.teams.set_member(platform.auth, platform.auth.subject, 'admin', 'revoked', expected_revision=0, key='revoke-catalog')
    assert (await client.get('/v1/graphs')).status_code == 404


async def test_counts_only_active_published_profiles_in_requesting_team(platform, client):
    other = (await platform.profiles.get_agent(platform.other_auth))['agent_id']
    await publish_graph(platform, platform.auth, platform.agent_id, 'sop.conditional')
    await publish_graph(platform, platform.other_auth, other, 'sop.conditional')
    response = await client.get('/v1/graphs')
    selected = next(item for item in response.json()['items'] if item['graph_id'] == 'sop.conditional')
    assert selected['agents_using'] == 1
    assert (await client.get('/v1/graphs/sop.conditional')).json()['agents_using'] == 1


async def test_viewer_can_read_list_and_detail(platform, client):
    await platform.teams.set_member(platform.auth, 'catalog-viewer', 'viewer', 'active',
        expected_revision=0, key='add-catalog-viewer')
    headers = {'Authorization': f'Bearer development:{platform.auth.team_id}:catalog-viewer'}
    assert (await client.get('/v1/graphs', headers=headers)).status_code == 200
    assert (await client.get('/v1/graphs/conversation.multi_turn', headers=headers)).status_code == 200


async def test_connector_scope_restricts_agent_counts(platform):
    from workbench_agent.api.graphs import _counts
    second = await platform.profiles.create_agent(platform.auth, 'other visible agent', key='catalog-second')
    await publish_graph(platform, platform.auth, platform.agent_id, 'conversation.single_turn')
    # The new draft is published explicitly; only active pointers count.
    draft = await platform.profiles.get_agent(platform.auth, second['agent_id'])
    saved = await platform.profiles.save_draft(platform.auth, second['agent_id'],
        ProfileDraft.model_validate({**draft['draft'], 'graph_id': 'conversation.single_turn'}), draft['revision'], key='catalog-save-second')
    await platform.profiles.publish(platform.auth, second['agent_id'], saved['revision'], key='catalog-publish-second')
    scoped = platform.auth.model_copy(update={'identity_kind': 'connector', 'agent_scope': (second['agent_id'],)})
    assert (await _counts(platform.backend, platform.auth))['conversation.single_turn'] == 2
    assert (await _counts(platform.backend, scoped))['conversation.single_turn'] == 1

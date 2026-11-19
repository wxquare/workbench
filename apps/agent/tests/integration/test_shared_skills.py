from uuid import uuid4

from workbench_agent.domain.profiles import ProfileDraft
from workbench_agent.application.errors import DomainError
import pytest


async def test_shared_skill_lifecycle_and_version_immutability(client, platform, tmp_path):
    client._transport.app.state.settings.storage.skill_object_root = tmp_path / 'skills'
    headers = {'Idempotency-Key': str(uuid4())}
    created = await client.post('/v1/skills', headers=headers, json={
        'name': 'value-investing', 'description': 'Investment analysis', 'content': 'Analyse cash flow.'})
    assert created.status_code == 201, created.text
    skill = created.json()
    skill_id = skill['skill_id']
    assert skill['revision'] == 0
    publish_headers = {'Idempotency-Key': str(uuid4()), 'If-Match': '0'}
    first = await client.post(f'/v1/skills/{skill_id}/publish', headers=publish_headers)
    assert first.status_code == 201, first.text
    assert first.json()['version'] == '1'
    replay = await client.post(f'/v1/skills/{skill_id}/publish', headers=publish_headers)
    assert replay.json() == first.json()
    client._transport.app.state.settings.storage.skill_object_root = None
    replay_without_storage = await client.post(f'/v1/skills/{skill_id}/publish', headers=publish_headers)
    assert replay_without_storage.status_code == 201
    assert replay_without_storage.json() == first.json()
    inaccessible = tmp_path / 'not-a-directory'
    inaccessible.write_text('not a directory')
    client._transport.app.state.settings.storage.skill_object_root = inaccessible
    replay_with_bad_storage = await client.post(f'/v1/skills/{skill_id}/publish', headers=publish_headers)
    assert replay_with_bad_storage.status_code == 201
    assert replay_with_bad_storage.json() == first.json()
    client._transport.app.state.settings.storage.skill_object_root = tmp_path / 'skills'
    saved = await client.patch(f'/v1/skills/{skill_id}', headers={'Idempotency-Key': str(uuid4()), 'If-Match': '1'},
                               json={'description': 'Updated', 'content': 'Analyse margins.'})
    assert saved.status_code == 200, saved.text
    second = await client.post(f'/v1/skills/{skill_id}/publish', headers={'Idempotency-Key': str(uuid4()), 'If-Match': '2'})
    assert second.status_code == 201, second.text
    assert second.json()['version'] == '2'
    old = await client.get(f'/v1/skills/{skill_id}/versions/1')
    assert 'Analyse cash flow.' in old.json()['content']
    agent = await platform.profiles.get_agent(platform.auth, platform.agent_id)
    duplicate = ProfileDraft.model_validate({**agent['draft'], 'skills': [first.json(), second.json()]})
    with pytest.raises(DomainError, match='DUPLICATE_SKILL'):
        await platform.profiles.save_draft(platform.auth, platform.agent_id, duplicate, agent['revision'], key=str(uuid4()))
    listed = await client.get('/v1/skills')
    assert any(item['skill_id'] == skill_id for item in listed.json()['items'])


async def test_shared_skill_rejects_foreign_team_and_duplicate_name(client, platform, tmp_path):
    client._transport.app.state.settings.storage.skill_object_root = tmp_path / 'skills'
    payload = {'name': 'analysis', 'description': 'Analysis', 'content': 'First'}
    response = await client.post('/v1/skills', headers={'Idempotency-Key': str(uuid4())}, json=payload)
    assert response.status_code == 201
    duplicate = await client.post('/v1/skills', headers={'Idempotency-Key': str(uuid4())}, json=payload)
    assert duplicate.status_code == 409
    foreign = {'Authorization': f'Bearer development:{platform.other_auth.team_id}:{platform.other_auth.subject}'}
    assert (await client.get(f"/v1/skills/{response.json()['skill_id']}", headers=foreign)).status_code == 404


async def test_shared_version_can_be_bound_to_two_agents_and_not_other_team(client, platform, tmp_path):
    client._transport.app.state.settings.storage.skill_object_root = tmp_path / 'skills'
    created = await client.post('/v1/skills', headers={'Idempotency-Key': str(uuid4())},
        json={'name': 'shared', 'description': 'Shared analysis', 'content': 'Use evidence.'})
    skill_id = created.json()['skill_id']
    published = await client.post(f'/v1/skills/{skill_id}/publish', headers={'If-Match': '0', 'Idempotency-Key': str(uuid4())})
    ref = published.json()
    second = await platform.profiles.create_agent(platform.auth, 'Second', key=str(uuid4()))
    for agent_id in (platform.agent_id, second['agent_id']):
        agent = await platform.profiles.get_agent(platform.auth, agent_id)
        draft = ProfileDraft.model_validate({**agent['draft'], 'graph_id': 'conversation.multi_turn', 'skills': [ref]})
        saved = await platform.profiles.save_draft(platform.auth, agent_id, draft, agent['revision'], key=str(uuid4()))
        await platform.profiles.publish(platform.auth, agent_id, saved['revision'], key=str(uuid4()))
        assert (await platform.contexts.resolve(platform.auth, agent_id)).skills[0].resource_id == ref['resource_id']
    async with platform.backend.create_uow().begin_read() as uow:
        other_agent = await platform.profiles.agent(uow, platform.other_auth)
    other = await platform.profiles.get_agent(platform.other_auth, other_agent['agent_id'])
    foreign = ProfileDraft.model_validate({**other['draft'], 'skills': [ref]})
    with pytest.raises(DomainError, match='NOT_FOUND'):
        await platform.profiles.save_draft(platform.other_auth, other_agent['agent_id'], foreign, other['revision'], key=str(uuid4()))


async def test_legacy_profile_remains_readable_but_new_draft_has_skill_limit(platform):
    agent = await platform.profiles.get_agent(platform.auth, platform.agent_id)
    ref = {'resource_id': 'old-version', 'version': '1', 'content_hash': 'a' * 64}
    draft = ProfileDraft.model_validate({**agent['draft'], 'skills': [ref] * 17})
    assert len(ProfileDraft.model_validate_json(draft.model_dump_json()).skills) == 17
    with pytest.raises(DomainError, match='TOO_MANY_SKILLS'):
        await platform.profiles.save_draft(platform.auth, platform.agent_id, draft, agent['revision'], key=str(uuid4()))


async def test_publish_requires_storage_and_conflicts_preserve_draft(client, platform):
    item = (await client.post('/v1/skills', headers={'Idempotency-Key': str(uuid4())},
        json={'name': 'safe', 'description': 'Safe', 'content': 'Original'})).json()
    skill_id = item['skill_id']
    missing = await client.post(f'/v1/skills/{skill_id}/publish', headers={'If-Match': '0', 'Idempotency-Key': str(uuid4())})
    assert missing.status_code == 503
    saved = await client.patch(f'/v1/skills/{skill_id}', headers={'If-Match': '0', 'Idempotency-Key': str(uuid4())},
        json={'description': 'Safe', 'content': 'Updated'})
    assert saved.status_code == 200
    stale = await client.patch(f'/v1/skills/{skill_id}', headers={'If-Match': '0', 'Idempotency-Key': str(uuid4())},
        json={'description': 'Safe', 'content': 'Stale'})
    assert stale.status_code == 409
    assert (await client.get(f'/v1/skills/{skill_id}')).json()['draft']['content'] == 'Updated'


async def test_viewer_can_read_but_cannot_create_or_publish(client, platform, tmp_path):
    client._transport.app.state.settings.storage.skill_object_root = tmp_path / 'skills'
    item = (await client.post('/v1/skills', headers={'Idempotency-Key': str(uuid4())},
        json={'name': 'team-skill', 'description': 'Team', 'content': 'Guidance'})).json()
    await platform.teams.set_member(platform.auth, 'skill-viewer', 'viewer', 'active', expected_revision=0, key=str(uuid4()))
    viewer = {'Authorization': f'Bearer development:{platform.auth.team_id}:skill-viewer'}
    assert (await client.get('/v1/skills', headers=viewer)).status_code == 200
    assert (await client.get(f"/v1/skills/{item['skill_id']}", headers=viewer)).status_code == 200
    response = await client.post('/v1/skills', headers={**viewer, 'Idempotency-Key': str(uuid4())},
        json={'name': 'new', 'description': 'New', 'content': 'No'})
    assert response.status_code == 403
    response = await client.post(f"/v1/skills/{item['skill_id']}/publish", headers={**viewer, 'If-Match': '0', 'Idempotency-Key': str(uuid4())})
    assert response.status_code == 403

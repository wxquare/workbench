import asyncio
from uuid import uuid4
import pytest
from workbench_agent.domain.identity import AuthContext
from workbench_agent.domain.profiles import ProfileDraft
from workbench_agent.application.errors import DomainError

async def test_cross_team_and_absent_resources_both_404(platform, client):
    other = await platform.profiles.get_agent(platform.other_auth)
    for resource in (other['agent_id'], str(uuid4())):
        response = await client.get(f'/v1/agents/{resource}')
        assert response.status_code == 404
        assert response.json() == {'code': 'NOT_FOUND'}

async def test_visible_agent_requires_current_maintainer_role(platform, client):
    await platform.teams.set_member(platform.auth, 'development-admin', 'viewer', 'active',
        expected_revision=0, key='demote')
    response = await client.post(f'/v1/agents/{platform.agent_id}/profiles/publish',
        headers={'If-Match': '0', 'Idempotency-Key': 'denied'})
    assert response.status_code == 403

async def test_revocation_invalidates_existing_context_and_bearer(platform, client):
    context = await platform.contexts.resolve(platform.auth, platform.agent_id)
    await platform.teams.set_member(platform.auth, 'development-admin', 'admin', 'revoked',
        expected_revision=0, key='revoke')
    assert (await client.get(f'/v1/agents/{platform.agent_id}')).status_code == 404
    with pytest.raises(DomainError):
        await platform.contexts.revalidate(context)

async def test_concurrent_draft_cas_has_one_winner(platform):
    current = await platform.profiles.get_agent(platform.auth, platform.agent_id)
    model = await platform.profiles.get_active_model(platform.auth)
    async def save():
        return await platform.profiles.save_draft(platform.auth, platform.agent_id,
            ProfileDraft(default_model=model.ref), expected_revision=current['revision'], key=str(uuid4()))
    results = await asyncio.gather(save(), save(), return_exceptions=True)
    assert sum(isinstance(item, dict) for item in results) == 1
    assert [item.code for item in results if isinstance(item, DomainError)] == ['REVISION_CONFLICT']

async def test_mutation_replay_and_mismatch_are_atomic(platform, client):
    model = await platform.profiles.get_active_model(platform.auth)
    body = {'default_model': model.ref.model_dump()}
    url = f'/v1/agents/{platform.agent_id}'
    headers = {'If-Match': str((await platform.profiles.get_agent(platform.auth,platform.agent_id))['revision']), 'Idempotency-Key': 'repeat'}
    first = await client.patch(url, json=body, headers=headers)
    again = await client.patch(url, json=body, headers=headers)
    assert first.status_code == again.status_code == 200
    assert first.json() == again.json()
    assert again.headers['Idempotency-Replayed'] == 'true'
    body['graph_id'] = 'another.v1'
    mismatch = await client.patch(url, json=body, headers=headers)
    assert mismatch.status_code == 409
    assert mismatch.json()['code'] == 'IDEMPOTENCY_CONFLICT'

async def test_replay_does_not_bypass_current_revocation(platform, client):
    model = await platform.profiles.get_active_model(platform.auth)
    url = f'/v1/agents/{platform.agent_id}'
    headers = {'If-Match': str((await platform.profiles.get_agent(platform.auth,platform.agent_id))['revision']), 'Idempotency-Key': 'revoked-replay'}
    body = {'default_model': model.ref.model_dump()}
    assert (await client.patch(url, json=body, headers=headers)).status_code == 200
    await platform.teams.set_member(platform.auth, platform.auth.subject, 'admin', 'revoked', expected_revision=0, key='revoke')
    assert (await client.patch(url, json=body, headers=headers)).status_code == 404

async def test_mutation_requires_headers_and_rejects_client_team_claim(platform, client):
    model = await platform.profiles.get_active_model(platform.auth)
    body = {'default_model': model.ref.model_dump()}
    url = f'/v1/agents/{platform.agent_id}'
    assert (await client.patch(url, json=body, headers={'Idempotency-Key': 'missing-match'})).status_code == 428
    assert (await client.patch(url, json=body, headers={'If-Match': '0'})).status_code == 422
    body['team_id'] = platform.other_auth.team_id
    assert (await client.patch(url, json=body, headers={'If-Match': '0', 'Idempotency-Key': 'team'})).status_code == 422

async def test_semantic_revision_is_in_idempotency_hash(platform, client):
    model = await platform.profiles.get_active_model(platform.auth)
    body = {'default_model': model.ref.model_dump()}
    url = f'/v1/agents/{platform.agent_id}'
    revision=(await platform.profiles.get_agent(platform.auth,platform.agent_id))['revision']
    assert (await client.patch(url, json=body, headers={'If-Match': str(revision), 'Idempotency-Key': 'semantic'})).status_code == 200
    response = await client.patch(url, json=body, headers={'If-Match': str(revision+1), 'Idempotency-Key': 'semantic'})
    assert response.status_code == 409
    assert response.json()['code'] == 'IDEMPOTENCY_CONFLICT'

async def test_team_member_api_isolated_and_idempotent(platform, client):
    url = f'/v1/teams/{platform.auth.team_id}/members/new-viewer'
    headers = {'If-Match': '0', 'Idempotency-Key': 'member'}
    response = await client.put(url, json={'role': 'viewer'}, headers=headers)
    assert response.status_code == 200
    replay = await client.put(url, json={'role': 'viewer'}, headers=headers)
    assert replay.headers['Idempotency-Replayed'] == 'true'
    assert (await client.get(url)).json()['role'] == 'viewer'
    assert (await client.get(f'/v1/teams/{platform.other_auth.team_id}/members/other-admin')).status_code == 404

async def test_new_agent_has_draft_and_requires_explicit_publish(platform, client):
    response = await client.post('/v1/agents', json={'name': 'new'}, headers={'Idempotency-Key': 'new'})
    assert response.status_code == 201
    agent_id = response.json()['agent_id']
    row = (await client.get(f'/v1/agents/{agent_id}')).json()
    assert row['active_profile_id'] is None
    published = await client.post(f'/v1/agents/{agent_id}/profiles/publish',
        headers={'If-Match': '0', 'Idempotency-Key': 'publish-new'})
    assert published.status_code == 201
    context = await platform.contexts.resolve(platform.auth, agent_id)
    assert context.default_model.version == '1'

async def test_new_team_is_seeded_with_usable_published_agent(platform, client):
    response = await client.post('/v1/teams', json={'name': 'second-team'}, headers={'Idempotency-Key': 'team'})
    assert response.status_code == 201
    row = response.json()
    auth = AuthContext(team_id=row['team_id'], subject=platform.auth.subject, roles=('admin',))
    context = await platform.contexts.resolve(auth, row['agent_id'])
    assert context.team_id == row['team_id']
    model = await platform.profiles.get_active_model(auth, row['agent_id'])
    assert model.model_id == 'deepseek-flash'

async def test_mutation_audit_and_receipt_are_committed_once(platform):
    model = await platform.profiles.get_active_model(platform.auth)
    draft = ProfileDraft(default_model=model.ref)
    revision=(await platform.profiles.get_agent(platform.auth,platform.agent_id))['revision']
    async with platform.backend.create_uow().begin_read() as uow:
        baseline=len(await platform.backend.configuration.rows(uow,'audit_logs',team_id=platform.auth.team_id,operation='agent.draft'))
    for _ in range(2):
        await platform.profiles.save_draft(platform.auth, platform.agent_id, draft, revision, key='audit')
    async with platform.backend.create_uow().begin_read() as uow:
        store = platform.backend.configuration
        audits = await store.rows(uow, 'audit_logs', team_id=platform.auth.team_id, operation='agent.draft')
        receipt = await store.one(uow, 'idempotency_records', team_id=platform.auth.team_id, key='audit')
        assert len(audits) == baseline+1
        assert receipt['expires_at'] - receipt['created_at'] == 259200000
        assert receipt['status_code'] == 200

async def test_connector_agent_pagination_applies_scope_before_limit(platform):
    created = await platform.profiles.create_agent(platform.auth, 'scoped', key='scope')
    target = max(platform.agent_id, created['agent_id'])
    auth = platform.auth.model_copy(update={'identity_kind': 'connector', 'agent_scope': (target,)})
    rows = await platform.profiles.list_agents(auth, limit=1)
    assert [row['agent_id'] for row in rows] == [target]

async def test_team_creation_can_replay_with_original_authenticated_team(platform, client):
    headers = {'Idempotency-Key': 'team-replay'}
    first = await client.post('/v1/teams', json={'name': 'idempotent-team'}, headers=headers)
    second = await client.post('/v1/teams', json={'name': 'idempotent-team'}, headers=headers)
    assert first.status_code == second.status_code == 201
    assert second.json() == first.json()
    assert second.headers['Idempotency-Replayed'] == 'true'

"""Team model library is independent of Agent configuration."""

from uuid import uuid4

from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient

from workbench_agent.api.app import create_app
from workbench_agent.settings import ModelCredentialBinding


async def test_team_library_creates_model_without_agent_and_publishes_for_binding(client, platform):
    root = '/v1/model-profiles'
    listed = await client.get(root)
    assert listed.status_code == 200
    assert [item['name'] for item in listed.json()['items']] == ['deepseek-dod-default']

    created = await client.post(
        root, json={'name': 'shared-model'}, headers={'Idempotency-Key': 'create-team-model'}
    )
    assert created.status_code == 201, created.text
    assert created.json()['active_version'] is None
    async with platform.backend.create_uow().begin_read() as uow:
        row = await platform.backend.configuration.one(
            uow, 'model_profiles', team_id=platform.auth.team_id, name='shared-model'
        )
    assert row['agent_id'] is None and row['scope'] == 'team'

    updated = await client.patch(
        root + '/shared-model',
        json={'model_id': 'deepseek-shared'},
        headers={'If-Match': '0', 'Idempotency-Key': 'edit-team-model'},
    )
    assert updated.status_code == 200
    published = await client.post(
        root + '/shared-model/publish',
        headers={'If-Match': '1', 'Idempotency-Key': 'publish-team-model'},
    )
    assert published.status_code == 201, published.text
    other = await platform.profiles.create_agent(platform.auth, 'other', key='other-agent')
    available = await client.get(f'/v1/agents/{other["agent_id"]}/model-profiles')
    assert 'shared-model' in [item['name'] for item in available.json()['items']]
    assert (await client.get(root + '/shared-model')).json()['active_ref'][
        'resource_id'
    ] == published.json()['model_version_id']
    from workbench_agent.domain.profiles import ProfileDraft, VersionRef

    detail = await platform.profiles.get_agent(platform.auth, other['agent_id'])
    draft = ProfileDraft.model_validate(detail['draft']).model_copy(
        update={
            'default_model': VersionRef.model_validate(
                (await client.get(root + '/shared-model')).json()['active_ref']
            )
        }
    )
    saved = await platform.profiles.save_draft(
        platform.auth, other['agent_id'], draft, detail['revision'], key='bind-shared-model'
    )
    assert saved['draft']['default_model']['resource_id'] == published.json()['model_version_id']


async def test_team_library_rejects_viewer_and_agent_scoped_identity(client, platform):
    await platform.teams.set_member(
        platform.auth, 'reader', 'viewer', 'active', expected_revision=0, key='reader'
    )
    viewer = {'Authorization': f'Bearer development:{platform.auth.team_id}:reader'}
    _scoped = {
        'Authorization': f'Bearer development:{platform.auth.team_id}:{platform.auth.subject}'
    }
    app = create_app(platform.settings, backend=platform.backend)
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url='http://test', headers=viewer
        ) as other:
            assert (await other.get('/v1/model-profiles')).status_code == 403
    restricted = platform.auth.model_copy(update={'agent_scope': (platform.agent_id,)})
    from workbench_agent.application.model_profiles import ModelProfileService
    from workbench_agent.application.errors import DomainError

    try:
        await ModelProfileService(platform.backend).list(restricted, None)
        assert False, 'agent-scoped identity accessed team library'
    except DomainError as error:
        assert error.status == 403


async def test_team_probe_chooses_authorized_agent_and_returns_durable_job(platform):
    binding = ModelCredentialBinding(
        team_id=platform.auth.team_id,
        agent_ids=(platform.agent_id,),
        base_url='https://api.deepseek.com',
        secret_ref='env://DEEPSEEK_API_KEY',
    )
    platform.settings.service.model_credential_bindings = (binding,)
    app = create_app(platform.settings, backend=platform.backend)
    headers = {
        'Authorization': f'Bearer development:{platform.auth.team_id}:{platform.auth.subject}'
    }
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url='http://test', headers=headers
        ) as client:
            submitted = await client.post(
                '/v1/model-profiles/deepseek-dod-default/probe',
                headers={'Idempotency-Key': str(uuid4())},
            )
            assert submitted.status_code == 202, submitted.text
            job = submitted.json()
            assert job['agent_id'] == platform.agent_id and job['status'] == 'queued'
            status = await client.get(f'/v1/agents/{job["agent_id"]}/admin-jobs/{job["job_id"]}')
            assert status.status_code == 200 and status.json()['status'] == 'queued'


async def test_team_probe_rejects_endpoint_without_authorized_agent(client):
    result = await client.post(
        '/v1/model-profiles/deepseek-dod-default/probe', headers={'Idempotency-Key': str(uuid4())}
    )
    assert result.status_code == 409
    assert result.json()['code'] == 'MODEL_PROFILE_UNAVAILABLE'


async def test_team_model_key_can_be_saved_without_choosing_agent(platform, monkeypatch):
    monkeypatch.setenv('WORKBENCH_MODEL_SECRET_MASTER_KEY', Fernet.generate_key().decode())
    platform.settings.service.model_credential_bindings = (
        ModelCredentialBinding(
            team_id=platform.auth.team_id,
            agent_ids=(platform.agent_id,),
            base_url='https://api.deepseek.com',
            secret_ref='env://DEEPSEEK_API_KEY',
        ),
    )
    app = create_app(platform.settings, backend=platform.backend)
    headers = {
        'Authorization': f'Bearer development:{platform.auth.team_id}:{platform.auth.subject}'
    }
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url='http://test', headers=headers
        ) as client:
            result = await client.put(
                '/v1/model-profiles/deepseek-dod-default/credential',
                json={'api_key': 'fictional-team-key'},
                headers={'If-Match': '0'},
            )
            assert result.status_code == 200, result.text
            assert result.json()['draft']['secret_ref'].startswith('secret://managed-model/')
            assert 'fictional-team-key' not in result.text

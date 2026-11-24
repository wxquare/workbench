import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient

from workbench_agent.api.app import create_app


def authorize(platform):
    from workbench_agent.settings import ModelCredentialBinding

    platform.settings.service.model_credential_bindings = (
        ModelCredentialBinding(
            team_id=platform.auth.team_id,
            agent_ids=(platform.agent_id,),
            base_url='https://api.deepseek.com',
            secret_ref='env://DEEPSEEK_API_KEY',
        ),
    )


async def test_admin_can_set_model_key_without_exposing_it(platform, tmp_path, monkeypatch):
    monkeypatch.setenv('WORKBENCH_MODEL_SECRET_MASTER_KEY', Fernet.generate_key().decode())
    authorize(platform)
    app = create_app(platform.settings, backend=platform.backend)
    url = f'/v1/agents/{platform.agent_id}/model-profiles/deepseek-dod-default/credential'
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url='http://test',
            headers={
                'Authorization': f'Bearer development:{platform.auth.team_id}:{platform.auth.subject}'
            },
        ) as client:
            response = await client.put(
                url, json={'api_key': 'fictional-test-secret'}, headers={'If-Match': '0'}
            )
            assert response.status_code == 200, response.text
            assert 'fictional-test-secret' not in response.text
            profile = response.json()
            assert profile['draft']['secret_ref'].startswith('secret://managed-model/')
            detail = await client.get(url.removesuffix('/credential'))
            assert 'fictional-test-secret' not in detail.text
            assert detail.json()['draft']['secret_ref'] == profile['draft']['secret_ref']
            stale = await client.put(
                url, json={'api_key': 'another-secret'}, headers={'If-Match': '0'}
            )
            assert stale.status_code == 409
            assert 'another-secret' not in stale.text
    assert b'fictional-test-secret' not in platform.settings.storage.database_path.read_bytes()
    async with platform.backend.create_uow().begin_read() as uow:
        audit = await platform.backend.configuration.rows(
            uow, 'audit_logs', team_id=platform.auth.team_id, operation='model.credential.update'
        )
        assert len(audit) == 1
        assert 'fictional-test-secret' not in str(audit)


async def test_model_key_rejects_other_team(platform, monkeypatch):
    monkeypatch.setenv('WORKBENCH_MODEL_SECRET_MASTER_KEY', Fernet.generate_key().decode())
    app = create_app(platform.settings, backend=platform.backend)
    url = f'/v1/agents/{platform.agent_id}/model-profiles/deepseek-dod-default/credential'
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url='http://test',
            headers={
                'Authorization': f'Bearer development:{platform.other_auth.team_id}:{platform.other_auth.subject}'
            },
        ) as client:
            response = await client.put(
                url, json={'api_key': 'fictional-test-secret'}, headers={'If-Match': '0'}
            )
            assert response.status_code in (403, 404)
            assert 'fictional-test-secret' not in response.text


async def test_published_managed_key_is_used_by_model_factory(platform, monkeypatch):
    from workbench_agent.application.model_profiles import ModelProfileService
    from workbench_agent.connectors.managed_model_secrets import configured_managed_model_secrets
    from workbench_agent.connectors.secrets import SecretProvider, SecretReferenceAvailability
    from workbench_agent.runtime.models.factory import ModelCredentialPolicy, ModelFactory

    monkeypatch.setenv('WORKBENCH_MODEL_SECRET_MASTER_KEY', Fernet.generate_key().decode())
    authorize(platform)
    store = configured_managed_model_secrets(platform.settings)
    policy = ModelCredentialPolicy(platform.settings.service.model_credential_bindings)
    service = ModelProfileService(platform.backend)
    updated = await service.set_credential(
        platform.auth,
        platform.agent_id,
        'deepseek-dod-default',
        'fictional-runtime-key',
        0,
        store,
        policy,
    )
    result = await service.publish(
        platform.auth,
        platform.agent_id,
        'deepseek-dod-default',
        updated['revision'],
        'publish-managed-key',
    )
    from workbench_agent.domain.profiles import ModelProfileVersion

    version = ModelProfileVersion.model_validate(result.body)
    assert SecretReferenceAvailability(store).available(version.secret_ref)
    assert not SecretReferenceAvailability().available(version.secret_ref)
    captured = {}
    monkeypatch.setattr(
        'workbench_agent.runtime.models.factory.ChatDeepSeek',
        lambda **kwargs: captured.update(kwargs),
    )
    ModelFactory(policy, managed_secrets=store).build(
        version, SecretProvider(), team_id=platform.auth.team_id, agent_id=platform.agent_id
    )
    assert captured['api_key'] == 'fictional-runtime-key'
    assert captured['base_url'] == 'https://api.deepseek.com'
    from workbench_agent.application.errors import DomainError

    with pytest.raises(DomainError, match='MODEL_PROFILE_UNAVAILABLE'):
        ModelFactory(policy, managed_secrets=store).build(
            version, SecretProvider(), team_id=platform.auth.team_id, agent_id='unauthorized-agent'
        )


async def test_unconfigured_managed_store_fails_closed(platform, monkeypatch):
    monkeypatch.delenv('WORKBENCH_MODEL_SECRET_MASTER_KEY', raising=False)
    app = create_app(platform.settings, backend=platform.backend)
    url = f'/v1/agents/{platform.agent_id}/model-profiles/deepseek-dod-default/credential'
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url='http://test',
            headers={
                'Authorization': f'Bearer development:{platform.auth.team_id}:{platform.auth.subject}'
            },
        ) as client:
            response = await client.put(
                url, json={'api_key': 'fictional-test-secret'}, headers={'If-Match': '0'}
            )
            assert response.status_code == 503
            assert response.json()['code'] == 'MODEL_CREDENTIAL_STORE_UNAVAILABLE'


async def test_managed_store_requires_deployment_authorization(platform, monkeypatch):
    monkeypatch.setenv('WORKBENCH_MODEL_SECRET_MASTER_KEY', Fernet.generate_key().decode())
    app = create_app(platform.settings, backend=platform.backend)
    url = f'/v1/agents/{platform.agent_id}/model-profiles/deepseek-dod-default/credential'
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url='http://test',
            headers={
                'Authorization': f'Bearer development:{platform.auth.team_id}:{platform.auth.subject}'
            },
        ) as client:
            response = await client.put(
                url, json={'api_key': 'fictional-test-secret'}, headers={'If-Match': '0'}
            )
            assert response.status_code == 409
            assert response.json()['code'] == 'MODEL_PROFILE_UNAVAILABLE'
            assert not (platform.settings.storage.persistent_root / 'model-secrets').exists()


async def test_managed_key_works_through_run_and_worker(platform, monkeypatch):
    from langchain_core.messages import AIMessage
    from workbench_agent.cli.worker import run_worker
    from workbench_agent.domain.profiles import ModelProfileVersion, ProfileDraft
    from support.runs import make_run_payload

    monkeypatch.setenv('WORKBENCH_MODEL_SECRET_MASTER_KEY', Fernet.generate_key().decode())
    authorize(platform)
    captured = []

    class FakeModel:
        def __init__(self, **kwargs):
            captured.append(kwargs['api_key'])

        async def ainvoke(self, messages, **kwargs):
            return AIMessage(
                content='Test answer',
                usage_metadata={'input_tokens': 10, 'output_tokens': 3, 'total_tokens': 13},
            )

    monkeypatch.setattr('workbench_agent.runtime.models.factory.ChatDeepSeek', FakeModel)
    app = create_app(platform.settings, backend=platform.backend)
    root = f'/v1/agents/{platform.agent_id}/model-profiles/deepseek-dod-default'
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url='http://test',
            headers={
                'Authorization': f'Bearer development:{platform.auth.team_id}:{platform.auth.subject}'
            },
        ) as client:
            key = await client.put(
                root + '/credential',
                json={'api_key': 'fictional-worker-key'},
                headers={'If-Match': '0'},
            )
            assert key.status_code == 200
            published = await client.post(
                root + '/publish', headers={'If-Match': '1', 'Idempotency-Key': 'publish-key'}
            )
            assert published.status_code == 201
            version = ModelProfileVersion.model_validate(published.json())
            agent = await platform.profiles.get_agent(platform.auth, platform.agent_id)
            draft = ProfileDraft(
                default_model=version.ref, graph_id='conversation.multi_turn', allowed_modes=('qa',)
            )
            changed = await platform.profiles.save_draft(
                platform.auth, platform.agent_id, draft, agent['revision'], key='draft-key'
            )
            await platform.profiles.publish(
                platform.auth, platform.agent_id, changed['revision'], key='publish-agent-key'
            )
            payload = make_run_payload(platform.agent_id, mode='qa')
            response = await client.post(
                '/v1/runs', json=payload, headers={'Idempotency-Key': 'managed-key-run'}
            )
            assert response.status_code == 202, response.text
            result = await run_worker(platform.settings, once=True)
            assert result['status'] == 'projected'
            run = await client.get('/v1/runs/' + response.json()['run_id'])
            assert run.json()['status'] == 'succeeded'
            assert 'Test answer' in str(run.json())
            assert captured == ['fictional-worker-key']

import pytest
from workbench_agent.settings import ModelProfile
from workbench_agent.cli.bootstrap import BootstrapService


async def test_bootstrap_does_not_replace_published_profile(platform):
    first = await platform.bootstrap()
    await platform.publish_model(model_id='team-approved-model')
    await platform.bootstrap()
    current = await platform.profiles.get_active_model(platform.auth)
    assert current.model_id == 'team-approved-model'
    assert first.default_model.name == 'deepseek-dod-default'
    assert current.secret_ref == 'env://DEEPSEEK_API_KEY'


async def test_seed_configuration_is_published_without_resolving_credentials(platform, monkeypatch):
    monkeypatch.setenv('DEEPSEEK_API_KEY', 'must-never-be-persisted')
    current = await platform.profiles.get_active_model(platform.auth)
    assert current.version == '1'
    assert current.configuration.base_url == 'https://api.deepseek.com'
    assert current.configuration.provider_options == {'thinking': {'type': 'disabled'}}
    assert 'must-never-be-persisted' not in current.model_dump_json()
    assert current.configuration.sdk_max_retries == 0


async def test_production_bootstrap_cannot_grant_admin_to_unverified_identity(platform):
    production = platform.settings.model_copy(update={'environment': 'production'})
    with pytest.raises(PermissionError):
        await BootstrapService(platform.backend).apply(production)


async def test_bootstrap_does_not_reactivate_revoked_admin(platform):
    await platform.teams.set_member(
        platform.auth, platform.auth.subject, 'admin', 'revoked', expected_revision=0, key='revoke'
    )
    await platform.bootstrap()
    from workbench_agent.application.errors import DomainError

    with pytest.raises(DomainError):
        await platform.profiles.get_active_model(platform.auth)


async def test_api_lifespan_never_creates_or_seeds_database(config_data):
    from workbench_agent.api.app import create_app
    from pathlib import Path
    from httpx import ASGITransport, AsyncClient
    from workbench_agent.settings import Settings

    settings = Settings.model_validate(config_data)
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            assert (await client.get('/health/ready')).status_code == 503
            assert not settings.storage.database_path.exists()


async def test_api_lifespan_does_not_seed_migrated_database(config_data):
    from workbench_agent.api.app import create_app
    from workbench_agent.settings import Settings
    from workbench_agent.cli.db import migrate
    from workbench_agent.persistence.backends.sqlite import SQLiteBackend

    settings = Settings.model_validate(config_data)
    migrate(settings.storage.database_url.get_secret_value())
    backend = SQLiteBackend(settings.storage.database_url.get_secret_value())
    app = create_app(settings, backend=backend)
    async with app.router.lifespan_context(app):
        async with backend.create_uow().begin_read() as uow:
            assert await backend.configuration.rows(uow, 'teams') == []
    await backend.close()


def test_standard_bootstrap_cli_requires_explicit_migration_and_is_repeatable(
    config_data, tmp_path
):
    import json
    import subprocess
    import sys
    import yaml
    from pathlib import Path

    config = tmp_path / 'config.yaml'
    config.write_text(yaml.safe_dump(config_data))

    def cli(*args):
        return subprocess.run(
            [sys.executable, '-m', 'workbench_agent.cli.main', *args, '--config', str(config)],
            text=True,
            capture_output=True,
            check=False,
        )

    missing = cli('bootstrap')
    assert missing.returncode == 1
    assert not (tmp_path / 'db.sqlite3').exists()
    assert cli('db', 'migrate').returncode == 0
    first, second = cli('bootstrap'), cli('bootstrap')
    assert first.returncode == second.returncode == 0
    assert json.loads(first.stdout)['created_agent'] is True
    assert json.loads(second.stdout)['created_agent'] is False
    assert json.loads(first.stdout)['default_model']['version'] == '1'


async def test_additional_create_if_missing_model_is_audited(platform):
    from workbench_agent.settings import LLMSettings

    llm = LLMSettings(profiles=[ModelProfile(), ModelProfile(name='additional-model')])
    settings = platform.settings.model_copy(update={'llm': llm})
    await BootstrapService(platform.backend).apply(settings)
    async with platform.backend.create_uow().begin_read() as uow:
        audits = await platform.backend.configuration.rows(
            uow, 'audit_logs', team_id=platform.auth.team_id, operation='bootstrap'
        )
        assert len(audits) == 2


async def test_published_model_and_agent_versions_have_database_timestamps(platform):
    await platform.publish_model('timestamped-model')
    async with platform.backend.create_uow().begin_read() as uow:
        models = await platform.backend.configuration.rows(
            uow, 'model_profile_versions', team_id=platform.auth.team_id
        )
        profiles = await platform.backend.configuration.rows(
            uow, 'runtime_profiles', team_id=platform.auth.team_id, status='published'
        )
        assert all(row['created_at'] > 1700000000000 for row in models + profiles)


# M2 model invocation coverage
import pytest


@pytest.mark.asyncio
async def test_model_draft_publish_and_durable_probe(client, platform):
    url = f'/v1/agents/{platform.agent_id}/model-profiles'
    initial = await client.get(url + '/deepseek-dod-default')
    assert initial.status_code == 200
    draft = initial.json()
    response = await client.patch(
        url + '/deepseek-dod-default',
        json={'temperature': 0.2},
        headers={'If-Match': str(draft['revision']), 'Idempotency-Key': 'patch-model'},
    )
    assert response.status_code == 200 and response.json()['draft']['temperature'] == 0.2
    response = await client.post(
        url + '/deepseek-dod-default/publish',
        headers={'If-Match': str(response.json()['revision']), 'Idempotency-Key': 'publish-model'},
    )
    assert response.status_code == 201
    response = await client.post(
        url + '/deepseek-dod-default/probe', headers={'Idempotency-Key': 'probe-model'}
    )
    assert response.status_code == 202 and response.json()['status'] == 'queued'
    assert await platform.count_rows('admin_jobs') == 1


@pytest.mark.asyncio
async def test_probe_job_executes_and_stores_versioned_report(platform):
    from workbench_agent.application.model_profiles import ModelProfileService
    from workbench_agent.runtime.models.deepseek import ModelProbe
    from workbench_agent.jobs.worker import ExecutorRegistry, Worker
    from dataclasses import asdict
    from langchain_core.messages import AIMessage, AIMessageChunk

    closed = []

    class Model:
        async def ainvoke(self, messages, **kwargs):
            if kwargs.get('response_format'):
                return AIMessage(
                    content='{"summary":"ok"}',
                    response_metadata={'model_name': 'actual', 'finish_reason': 'stop'},
                    usage_metadata={'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2},
                )
            return AIMessage(
                content='ok',
                response_metadata={'model_name': 'actual'},
                usage_metadata={'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2},
            )

        async def astream(self, messages):
            try:
                yield AIMessageChunk(content='ok')
            finally:
                closed.append(True)

        def bind_tools(self, *args, **kwargs):
            class ToolModel:
                async def ainvoke(self, messages):
                    return AIMessage(
                        content='',
                        tool_calls=[
                            {'name': 'local_sum', 'args': {'a': 2, 'b': 3}, 'id': 'call-1'}
                        ],
                    )

            return ToolModel()

    class Factory:
        def build(self, *args, **kwargs):
            return Model()

    service = ModelProfileService(platform.backend)
    job = await service.probe(
        platform.auth, platform.agent_id, 'deepseek-dod-default', 'probe-worker'
    )
    registry = ExecutorRegistry()
    registry.register_admin(
        'model_probe', ModelProbe(platform.backend, Factory(), object()).execute
    )
    assert (
        await Worker(platform.backend, 'probe-worker', registry).run_admin_once()
        == 'admin_completed'
    )
    from workbench_agent.domain.admin_jobs import AdminAuth
    from workbench_agent.jobs.admin import AdminJobService
    import json

    saved = await AdminJobService(platform.backend).get(
        AdminAuth(platform.auth.team_id, platform.agent_id, platform.auth.subject, True), job.job_id
    )
    report = json.loads(saved.result_ref)
    assert report['response_model'] == 'actual' and all(report['capabilities'].values())
    assert (
        report['capabilities']['client_stream_close']
        and closed
        and report['remote_cancel'] == 'unknown'
    )


@pytest.mark.asyncio
async def test_admission_rejects_missing_secret_reference_without_reading_value(
    platform, monkeypatch
):
    from workbench_agent.application.errors import DomainError

    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    with pytest.raises(DomainError, match='MODEL_PROFILE_UNAVAILABLE'):
        await platform.create_run()
    assert await platform.count_rows('runs') == 0


@pytest.mark.asyncio
async def test_probe_job_can_be_read_in_agent(client, platform):
    url = f'/v1/agents/{platform.agent_id}/model-profiles/deepseek-dod-default/probe'
    submitted = await client.post(url, headers={'Idempotency-Key': 'probe-read'})
    job_id = submitted.json()['job_id']
    result = await client.get(f'/v1/agents/{platform.agent_id}/admin-jobs/{job_id}')
    assert result.status_code == 200 and result.json()['status'] == 'queued'


@pytest.mark.asyncio
async def test_publish_replay_uses_original_draft_after_later_edit(client, platform):
    url = f'/v1/agents/{platform.agent_id}/model-profiles/deepseek-dod-default'
    first = await client.post(
        url + '/publish', headers={'If-Match': '0', 'Idempotency-Key': 'first-publish'}
    )
    assert first.status_code == 201
    edit = await client.patch(
        url, json={'temperature': 0.3}, headers={'If-Match': '1', 'Idempotency-Key': 'later-edit'}
    )
    assert edit.status_code == 200
    replay = await client.post(
        url + '/publish', headers={'If-Match': '0', 'Idempotency-Key': 'first-publish'}
    )
    assert replay.status_code == 201 and replay.json() == first.json()


@pytest.mark.asyncio
async def test_probe_records_credential_unavailable(platform):
    from workbench_agent.application.model_profiles import ModelProfileService
    from workbench_agent.runtime.models.deepseek import ModelProbe
    from workbench_agent.jobs.worker import ExecutorRegistry, Worker
    from workbench_agent.runtime.models.factory import ModelFactory
    from workbench_agent.connectors.secrets import SecretProvider
    from workbench_agent.domain.admin_jobs import AdminAuth
    from workbench_agent.jobs.admin import AdminJobService
    import json

    class MissingSecret:
        def resolve(self, ref):
            from workbench_agent.application.errors import DomainError

            raise DomainError('MODEL_CREDENTIAL_UNAVAILABLE', 409)

    job = await ModelProfileService(platform.backend).probe(
        platform.auth, platform.agent_id, 'deepseek-dod-default', 'missing-probe'
    )
    registry = ExecutorRegistry()
    from workbench_agent.runtime.models.factory import ModelCredentialPolicy

    policy = ModelCredentialPolicy(
        [
            {
                'team_id': platform.auth.team_id,
                'agent_ids': [platform.agent_id],
                'base_url': 'https://api.deepseek.com',
                'secret_ref': 'env://DEEPSEEK_API_KEY',
            }
        ]
    )
    registry.register_admin(
        'model_probe', ModelProbe(platform.backend, ModelFactory(policy), MissingSecret()).execute
    )
    assert (
        await Worker(platform.backend, 'probe-worker', registry).run_admin_once()
        == 'admin_completed'
    )
    saved = await AdminJobService(platform.backend).get(
        AdminAuth(platform.auth.team_id, platform.agent_id, platform.auth.subject, True), job.job_id
    )
    assert json.loads(saved.result_ref)['configuration'] == 'credential_unavailable'


@pytest.mark.asyncio
async def test_probe_replay_remains_on_original_version_after_publish(client, platform):
    url = f'/v1/agents/{platform.agent_id}/model-profiles/deepseek-dod-default'
    first = await client.post(url + '/probe', headers={'Idempotency-Key': 'stable-probe'})
    assert first.status_code == 202
    published = await client.post(
        url + '/publish', headers={'If-Match': '0', 'Idempotency-Key': 'new-version'}
    )
    assert published.status_code == 201
    replay = await client.post(url + '/probe', headers={'Idempotency-Key': 'stable-probe'})
    assert replay.status_code == 202 and replay.json()['job_id'] == first.json()['job_id']

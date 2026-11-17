import os
import pytest
from httpx import AsyncClient, ASGITransport
from workbench_agent.api.app import create_app
from workbench_agent.cli.worker import run_worker
from workbench_agent.cli.scheduler import run_scheduler

@pytest.mark.asyncio
@pytest.mark.parametrize('policy',['absent','outside','access','nonlocal','production_temp'])
async def test_actual_runtime_rejects_unsafe_storage(platform,tmp_path,monkeypatch,policy):
    settings=platform.settings.model_copy(deep=True)
    if policy=='absent': settings.storage.persistent_root=None
    elif policy=='outside': settings.storage.persistent_root=tmp_path/'outside'
    elif policy=='access': monkeypatch.setattr(os,'access',lambda *_:False)
    elif policy=='nonlocal':
        monkeypatch.setattr(os,'ST_LOCAL',0x100000000,raising=False)
    else:
        from pathlib import Path
        original=Path.resolve
        # Map the documented /tmp policy boundary to the mandated scratch root.
        monkeypatch.setattr(Path,'resolve',lambda self,*a,**kw: tmp_path if str(self)=='/tmp' else original(self,*a,**kw))
        settings.environment='production'
        settings.service.auth_mode='oidc'
        settings.service.oidc_issuer_ref='env://TEST_STORAGE_ISSUER'
        monkeypatch.setenv('TEST_STORAGE_ISSUER','https://issuer.example')
    assert await platform.backend.health()
    app=create_app(settings,backend=platform.backend)
    async with app.router.lifespan_context(app):
        assert not app.state.storage_ready
        async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as client:
            assert (await client.get('/health/ready')).status_code==503
            assert (await client.post('/v1/runs',json={})).status_code==503
    assert (await run_worker(settings,once=True))['ready'] is False
    assert (await run_scheduler(settings,once=True))['ready'] is False

@pytest.mark.asyncio
async def test_readiness_and_admission_recheck_policy_after_startup(platform):
    settings=platform.settings.model_copy(deep=True)
    app=create_app(settings,backend=platform.backend)
    async with app.router.lifespan_context(app):
        assert app.state.storage_ready
        settings.storage.persistent_root=None
        async with AsyncClient(transport=ASGITransport(app=app),base_url='http://test') as client:
            assert (await client.get('/health/ready')).status_code==503
            assert (await client.post('/v1/runs',json={})).status_code==503
    assert await platform.count_rows('runs')==0

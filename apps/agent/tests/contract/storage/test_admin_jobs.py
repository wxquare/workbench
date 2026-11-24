from dataclasses import replace
import pytest
from sqlalchemy import text
from test_uow import backend as backend


async def setup_auth(backend):
    from workbench_agent.domain.admin_jobs import AdminAuth

    async with backend.create_uow().begin_write() as uow:
        await uow.execute(
            text("INSERT INTO teams(team_id,name,status) VALUES ('team','team','active')")
        )
        await uow.execute(
            text("INSERT INTO agents(team_id,agent_id,name) VALUES ('team','agent','agent')")
        )
    return AdminAuth(team_id='team', agent_id='agent', subject='admin', can_manage=True)


async def test_submit_replays_and_rejects_changed_payload(backend):
    from workbench_agent.jobs.admin import AdminJobService
    from workbench_agent.persistence.errors import StorageError

    auth = await setup_auth(backend)
    service = AdminJobService(backend)
    first = await service.submit(auth, 'repo_sync', {'repo': 'a'}, 'key')
    replay = await service.submit(auth, 'repo_sync', {'repo': 'a'}, 'key')
    assert replay.job_id == first.job_id
    assert (await service.get(auth, first.job_id)).status == 'queued'
    with pytest.raises(StorageError) as error:
        await service.submit(auth, 'repo_sync', {'repo': 'b'}, 'key')
    assert error.value.code == 'UNIQUE_CONFLICT'
    with pytest.raises(LookupError):
        await service.get(replace(auth, team_id='other'), first.job_id)
    with pytest.raises(PermissionError):
        await service.submit(replace(auth, can_manage=False), 'repo_sync', {}, 'new')


async def test_guard_rejects_old_owner_and_atomic_result_rolls_back(backend):
    from workbench_agent.jobs.admin import AdminJobService
    from workbench_agent.persistence.errors import StorageError

    auth = await setup_auth(backend)
    job = await AdminJobService(backend).submit(auth, 'repo_sync', {}, 'key')
    async with backend.create_uow().begin_write() as uow:
        claim = await backend.admin_jobs.claim(uow, {'repo_sync'}, 'worker-a')
    assert claim.job_id == job.job_id
    async with backend.create_uow().begin_write() as uow:
        assert await backend.admin_jobs.claim(uow, {'repo_sync'}, 'worker-b') is None
    stale = replace(claim, worker_id='worker-b')
    with pytest.raises(StorageError) as error:
        async with backend.create_uow().begin_write() as uow:
            await backend.admin_jobs.finish(uow, stale, 'result')
    assert error.value.code == 'STALE_EXECUTION'
    assert (await AdminJobService(backend).get(auth, job.job_id)).status == 'running'
    async with backend.create_uow().begin_write() as uow:
        await backend.admin_jobs.finish(uow, claim, 'result')
    view = await AdminJobService(backend).get(auth, job.job_id)
    assert view.status == 'succeeded' and view.result_ref == 'result'
    async with backend.create_uow().begin_read() as uow:
        assert (await uow.execute(text('SELECT count(*) FROM admin_job_events'))).scalar_one() == 3


async def test_management_guard_rejects_non_admin_claim_and_expired_owner(backend):
    from workbench_agent.jobs.admin import AdminJobService, AdminJobGuard
    from workbench_agent.persistence.errors import StorageError

    auth = await setup_auth(backend)
    await AdminJobService(backend).submit(auth, 'model_probe', {}, 'probe-key')
    async with backend.create_uow().begin_write() as uow:
        claim = await backend.admin_jobs.claim(uow, {'model_probe'}, 'worker-a')
    guard = AdminJobGuard(backend.admin_jobs)
    with pytest.raises(TypeError):
        async with backend.create_uow().begin_write() as uow:
            await guard.validate(uow, object())
    async with backend.create_uow().begin_write() as uow:
        await backend.admin_jobs.renew(uow, claim)
    async with backend.create_uow().begin_write() as uow:
        await uow.execute(
            text('UPDATE admin_jobs SET lease_until=0 WHERE job_id=:id'), {'id': claim.job_id}
        )
    with pytest.raises(StorageError) as error:
        async with backend.create_uow().begin_write() as uow:
            await guard.validate(uow, claim)
    assert error.value.code == 'STALE_EXECUTION'
    async with backend.create_uow().begin_write() as uow:
        replacement = await backend.admin_jobs.claim(uow, {'model_probe'}, 'worker-a')
    assert replacement.attempt == claim.attempt + 1
    with pytest.raises(StorageError):
        async with backend.create_uow().begin_write() as uow:
            await backend.admin_jobs.finish(uow, claim, 'old-result')
    async with backend.create_uow().begin_write() as uow:
        await backend.admin_jobs.finish(uow, replacement, 'new-result')

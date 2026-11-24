import pytest
from support.interrupts import InterruptPlatform


@pytest.mark.asyncio
async def test_running_cancel_stops_coroutine_and_fences_late_result(platform):
    p = InterruptPlatform(platform)
    await p.running_cancel_race()


@pytest.mark.asyncio
async def test_wait_expiry_and_wall_deadline_do_not_fabricate_partial(platform):
    p = InterruptPlatform(platform)
    run, w = await p.waiting_run()
    assert w['expires_at_ms'] - w['created_at_ms'] == 24 * 3600 * 1000
    await p.expire(run)
    assert (await platform.get_run(run.run_id)).status == 'expired'
    assert (await platform.get_run(run.run_id)).outcome is None


@pytest.mark.asyncio
async def test_cancel_waiting_blocks_resume_and_releases_logical_slot(platform):
    from workbench_agent.application.errors import DomainError
    from workbench_agent.persistence import schema as s
    from support.jobs import row

    p = InterruptPlatform(platform)
    run, w = await p.waiting_run()
    assert (await p.service.cancel(platform.auth, run.run_id, 'cancel')).status == 'cancelled'
    with pytest.raises(DomainError):
        await p.resume(run, w, key='late')
    assert (await row(platform, s.threads, thread_id=run.thread_id))['active_run_id'] is None
    assert await p.service.cancel(platform.auth, run.run_id, 'cancel')


@pytest.mark.asyncio
async def test_cancel_finalizing_is_explicit_conflict(platform):
    from workbench_agent.application.errors import DomainError
    from sqlalchemy import update
    from workbench_agent.persistence import schema as s

    p = InterruptPlatform(platform)
    run = await platform.create_run()
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(
            update(s.runs).where(s.runs.c.run_id == run.run_id).values(status='finalizing')
        )
    with pytest.raises(DomainError) as error:
        await p.service.cancel(platform.auth, run.run_id, 'cancel')
    assert error.value.code == 'FINALIZATION_STARTED'


@pytest.mark.asyncio
async def test_public_cancel_and_resume_race_never_reopens_terminal_run(platform):
    import asyncio
    from support.jobs import row
    from workbench_agent.persistence import schema as s

    p = InterruptPlatform(platform)
    run, w = await p.waiting_run()
    _results = await asyncio.gather(
        p.resume(run, w, key='answer'),
        p.service.cancel(platform.auth, run.run_id, 'cancel'),
        return_exceptions=True,
    )
    assert (await platform.get_run(run.run_id)).status == 'cancelled'
    assert await p.worker.run_once() == 'idle'
    assert (await row(platform, s.threads, thread_id=run.thread_id))['active_run_id'] is None


@pytest.mark.asyncio
async def test_operation_boundary_rechecks_cancellation_after_authorization(platform):
    from workbench_agent.jobs.worker import Execution
    from support.jobs import claim
    from workbench_agent.persistence.errors import StaleExecution
    from workbench_agent.application.runs import RunService

    run = await platform.create_run()
    owned = await claim(platform)

    class CancellingExecution(Execution):
        async def _require_current(self, statuses):
            await super()._require_current(statuses)
            await RunService(platform.backend).cancel(
                platform.auth, run.run_id, 'cancel-at-boundary'
            )

    with pytest.raises(StaleExecution):
        await CancellingExecution(platform.backend, owned).check_operation()

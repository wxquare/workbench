import pytest
from sqlalchemy import update
from workbench_agent.persistence import schema as s
from support.interrupts import InterruptPlatform
from support.jobs import claim,expire_lease,row
from workbench_agent.domain.transitions import WAIT_APPROVAL_MS
from workbench_agent.application.errors import DomainError
from workbench_agent.jobs.deadlines import DeadlineService

@pytest.mark.asyncio
async def test_active_budget_pauses_wait_survives_resume_and_takeover(platform):
    p=InterruptPlatform(platform);run,w=await p.waiting_run()
    original=await row(platform,s.runs,run_id=run.run_id)
    assert original['active_since'] is None
    assert original['started_at'] is not None
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(update(s.runs).where(s.runs.c.run_id==run.run_id).values(active_elapsed_ms=170000))
    await p.resume(run,w,key='resume')
    resumed=await claim(platform)
    running=await row(platform,s.runs,run_id=run.run_id)
    assert running['deadline_at']-running['active_since']==10000
    assert running['wall_deadline_at']==original['wall_deadline_at']
    assert running['started_at']==original['started_at']
    await expire_lease(platform,resumed.guard)
    first=(await platform.get_run(run.run_id)).active_elapsed_ms
    assert first==170000
    recovered=await claim(platform,'takeover')
    again=await row(platform,s.runs,run_id=run.run_id)
    assert again['active_elapsed_ms']==170000
    assert again['started_at']==original['started_at']

@pytest.mark.asyncio
async def test_approval_deadline_and_expired_resume(platform):
    p=InterruptPlatform(platform);run,w=await p.waiting_run(approval=True)
    assert w['expires_at_ms']-w['created_at_ms']==WAIT_APPROVAL_MS
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(update(s.interrupts).where(s.interrupts.c.run_id==run.run_id).values(expires_at=0))
    with pytest.raises(DomainError) as error: await p.resume(run,w,key='late')
    assert error.value.code=='INTERRUPT_EXPIRED'

@pytest.mark.asyncio
async def test_waiting_total_wall_deadline_is_expired(platform):
    p=InterruptPlatform(platform);run,w=await p.waiting_run()
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(update(s.runs).where(s.runs.c.run_id==run.run_id).values(wall_deadline_at=0))
    assert await DeadlineService(platform.backend).scan()==1
    view=await platform.get_run(run.run_id)
    assert view.status=='expired' and view.outcome is None
    assert (await row(platform,s.threads,thread_id=run.thread_id))['active_run_id'] is None

@pytest.mark.asyncio
async def test_no_new_operation_after_active_deadline(platform):
    from workbench_agent.jobs.worker import Execution
    from workbench_agent.persistence.errors import StaleExecution
    run=await platform.create_run();owned=await claim(platform)
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(update(s.runs).where(s.runs.c.run_id==run.run_id).values(deadline_at=0))
    with pytest.raises(StaleExecution): await Execution(platform.backend,owned).check_operation()

@pytest.mark.asyncio
async def test_deadline_scan_without_owner_cannot_invalidate_running_execution(platform):
    from workbench_agent.persistence.errors import StaleExecution
    from dataclasses import replace
    run=await platform.create_run();owned=await claim(platform)
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(update(s.runs).where(s.runs.c.run_id==run.run_id).values(deadline_at=0))
    deadlines=DeadlineService(platform.backend)
    assert await deadlines.scan()==0
    assert (await row(platform,s.runs,run_id=run.run_id))['status']=='running'
    with pytest.raises(StaleExecution): await deadlines.expire_owned(replace(owned.guard,run_epoch=0))
    assert await deadlines.expire_owned(owned.guard)
    assert (await row(platform,s.runs,run_id=run.run_id))['status']=='timed_out'


@pytest.mark.asyncio
@pytest.mark.parametrize('executing',[False,True])
async def test_total_wall_expiry_takes_precedence_over_active_deadline(platform,executing):
    run=await platform.create_run();owned=await claim(platform) if executing else None
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(update(s.runs).where(s.runs.c.run_id==run.run_id).values(wall_deadline_at=0,deadline_at=0))
    service=DeadlineService(platform.backend)
    if executing: assert await service.expire_owned(owned.guard)
    else: assert await service.scan()==1
    view=await platform.get_run(run.run_id)
    assert view.status=='expired' and view.outcome is None

@pytest.mark.asyncio
async def test_owned_active_budget_only_is_timed_out_without_outcome(platform):
    run=await platform.create_run();owned=await claim(platform)
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(update(s.runs).where(s.runs.c.run_id==run.run_id).values(deadline_at=0))
    assert await DeadlineService(platform.backend).expire_owned(owned.guard)
    view=await platform.get_run(run.run_id)
    assert view.status=='timed_out' and view.outcome is None

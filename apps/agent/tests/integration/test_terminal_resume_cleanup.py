import pytest
from sqlalchemy import update, select
from support.interrupts import InterruptPlatform
from support.jobs import claim, row
from workbench_agent.jobs.worker import Execution
from workbench_agent.jobs.deadlines import DeadlineService
from workbench_agent.persistence import schema as s

@pytest.mark.asyncio
@pytest.mark.parametrize('terminal',['cancelled','expired'])
@pytest.mark.parametrize('phase',['submitted','applying_before','applying_after','applied'])
async def test_terminal_transition_settles_selected_resume_truthfully(platform,terminal,phase):
    p=InterruptPlatform(platform);run,w=await p.waiting_run()
    await p.resume(run,w,key='answer')
    request=await row(platform,s.resume_requests,run_id=run.run_id)
    owned=None
    if phase!='submitted':
        owned=await claim(platform)
        if phase=='applying_before':
            async with platform.backend.create_uow().begin_write() as uow:
                await uow.execute(update(s.resume_requests).where(s.resume_requests.c.request_id==request['request_id']).values(status='applying'))
        else:
            await p.worker.registry._executors['conversation.multi_turn'].apply_resume(Execution(platform.backend,owned),mark=phase=='applied')
    if terminal=='cancelled':
        await p.service.cancel(platform.auth,run.run_id,'cancel')
        if owned:
            async with platform.backend.create_uow().begin_write() as uow:
                await platform.backend.controls.finish_cancel(uow,owned.guard)
    else:
        async with platform.backend.create_uow().begin_write() as uow:
            await uow.execute(update(s.runs).where(s.runs.c.run_id==run.run_id).values(wall_deadline_at=0))
        deadlines=DeadlineService(platform.backend)
        if owned: assert await deadlines.expire_owned(owned.guard)
        else: assert await deadlines.scan()==1
    assert (await platform.get_run(run.run_id)).status==terminal
    request_after=await row(platform,s.resume_requests,run_id=run.run_id)
    interrupt=await row(platform,s.interrupts,run_id=run.run_id)
    applied=phase in ('applying_after','applied')
    assert request_after['status']==('applied' if applied else 'rejected')
    assert interrupt['status']==('consumed' if applied else terminal)
    assert interrupt['accepted_resume_request_id']==request['request_id']
    assert await p.saved_value(run,'last_applied_resume_id')==(request['request_id'] if applied else None)
    assert await p.worker.run_once()=='idle'
    if owned:
        from workbench_agent.persistence.errors import StaleExecution
        with pytest.raises(StaleExecution):
            async with platform.backend.create_uow().begin_write() as uow:
                await platform.backend.execution_events.append_execution(uow,owned.guard,'phase.changed',{'phase':'late'})
    async with platform.backend.create_uow().begin_read() as uow:
        events=(await uow.execute(select(s.run_events.c.type).where(s.run_events.c.run_id==run.run_id))).scalars().all()
    assert events.count('run.'+terminal)==1 and 'run.completed' not in events
    assert (await row(platform,s.threads,thread_id=run.thread_id))['active_run_id'] is None

@pytest.mark.asyncio
async def test_ambiguous_applying_evidence_rolls_back_terminal_mutations(platform):
    from sqlalchemy import delete
    p=InterruptPlatform(platform);run,w=await p.waiting_run();await p.resume(run,w,key='ambiguous')
    owned=await claim(platform)
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(update(s.resume_requests).where(s.resume_requests.c.run_id==run.run_id).values(status='applying'))
        await uow.execute(delete(s.graph_pending_writes).where(s.graph_pending_writes.c.run_id==run.run_id))
    await p.service.cancel(platform.auth,run.run_id,'cancel')
    with pytest.raises(ValueError,match='named interrupt'):
        async with platform.backend.create_uow().begin_write() as uow:
            await platform.backend.controls.finish_cancel(uow,owned.guard)
    assert (await platform.get_run(run.run_id)).status=='cancel_requested'
    assert (await row(platform,s.resume_requests,run_id=run.run_id))['status']=='applying'
    assert (await row(platform,s.interrupts,run_id=run.run_id))['status']=='response_submitted'
    assert (await row(platform,s.threads,thread_id=run.thread_id))['active_run_id']==run.run_id

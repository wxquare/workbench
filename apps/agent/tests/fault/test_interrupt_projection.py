import pytest
from workbench_agent.jobs.worker import Execution
from support.interrupts import InterruptPlatform
from support.jobs import claim,expire_lease,row
from workbench_agent.persistence import schema as s

@pytest.mark.asyncio
async def test_interrupt_checkpoint_without_projection_is_recovered_at_attempt_three(platform):
    from workbench_agent.jobs.reconciler import Reconciler
    from sqlalchemy import update
    p=InterruptPlatform(platform);await p.publish_v2();run=await platform.create_run();owned=await claim(platform)
    executor=p.worker.registry._executors['dod.v2'];graph=executor.registry.resolve('dod.v2','dod-state-v2')
    with executor.guard_provider.bind(owned.guard):
        await graph.ainvoke({'run_id':run.run_id,'input_text':'question','requires_input':True,'actor':platform.auth.subject,'messages':[]},
            {'configurable':{'thread_id':run.run_id}},context=Execution(platform.backend,owned),durability='sync')
    await expire_lease(platform,owned.guard)
    async with platform.backend.create_uow().begin_write() as uow: await uow.execute(update(s.run_jobs).values(attempt=3))
    decision=await Reconciler(platform.backend,p.worker.registry).recover(run.run_id)
    assert decision.action=='waiting'
    assert (await platform.get_run(run.run_id)).status=='waiting_input'
    assert (await row(platform,s.run_jobs,job_id=owned.guard.job_id))['attempt']==3

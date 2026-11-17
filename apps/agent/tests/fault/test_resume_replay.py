import pytest
from support.interrupts import InterruptPlatform

@pytest.mark.asyncio
async def test_resume_marker_survives_crash_before_business_mark(platform):
    p=InterruptPlatform(platform); run,w=await p.waiting_run()
    await p.resume(run,w,key='once')
    await p.apply_then_crash(run)
    assert await p.saved_value(run,'last_applied_resume_id')
    await p.recover_applied(run)
    assert await p.resume_status(run)=='applied'
    assert await p.saved_value(run,'input_text')=='ID'

@pytest.mark.asyncio
async def test_marker_commits_before_downstream_operation_and_resume_command_is_once(platform):
    from langgraph.types import Command
    from workbench_agent.jobs.worker import Execution
    from support.jobs import claim,expire_lease
    p=InterruptPlatform(platform);run,w=await p.waiting_run()
    await p.resume(run,w,key='once')
    executor=p.worker.registry._executors['dod.v2']
    graph=executor.registry.resolve('dod.v2','dod-state-v2')
    original=graph.ainvoke;commands=[]
    async def observe(value,*args,**kwargs):
        if isinstance(value,Command): commands.append(value)
        return await original(value,*args,**kwargs)
    graph.ainvoke=observe
    owned=await claim(platform)
    await executor.apply_resume(Execution(platform.backend,owned),mark=False)
    marker=await p.saved_value(run,'last_applied_resume_id')
    assert marker and await p.saved_value(run,'final_progress') is None
    await expire_lease(platform,owned.guard)
    owned=await claim(platform,'replacement')
    checked=[]
    class ObservedExecution(Execution):
        async def check_operation(self):
            await super().check_operation()
            # The only next graph node is compose; input has committed already.
            assert await p.saved_value(run,'last_applied_resume_id')==marker
            checked.append('durable-before-operation')
    await executor.execute(ObservedExecution(platform.backend,owned))
    assert checked and len(commands)==1
    assert (await p.saved_value(run,'final_progress'))['completed'] is True

@pytest.mark.asyncio
async def test_resume_applier_rejects_checkpoint_with_different_named_interrupt():
    from workbench_agent.application.resume import ResumeApplier
    request={'request_id':'r','graph_interrupt_id':'expected','response':'{"kind":"user_input","values":{"region":"ID"}}'}
    with pytest.raises(ValueError): ResumeApplier().apply(request,{'values':{},'interrupts':[]})

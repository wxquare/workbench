import pytest
from workbench_agent.jobs.worker import Execution
from support.interrupts import InterruptPlatform
from support.jobs import claim,expire_lease,row
from workbench_agent.persistence import schema as s

@pytest.mark.asyncio
async def test_interrupt_checkpoint_without_projection_is_recovered_at_attempt_three(platform):
    from workbench_agent.jobs.reconciler import Reconciler
    from workbench_agent.jobs.worker import DeploymentConnectorPolicy
    from workbench_agent.runtime.graphs.registry import foundation_worker_registry
    from integration.test_synthetic_alert_v2 import event, ingress, publish_v13, signed
    from sqlalchemy import update
    await publish_v13(platform)
    source=event();run=await ingress(platform).accept(*signed(source));owned=await claim(platform)
    registry=foundation_worker_registry(platform.backend)
    executor=registry._executors['rca.synthetic'];graph=executor.registry.resolve('rca.synthetic','dod-state-v13')
    policy=DeploymentConnectorPolicy(lambda:{'v2-test':{'subject':platform.auth.subject,
        'team_id':platform.auth.team_id,'agent_ids':[platform.agent_id]}})
    business_context={key:source[key] for key in ('region','service','environment','anchor','window_seconds')}
    business_context['link_region']='PH'
    with executor.guard_provider.bind(owned.guard):
        await graph.ainvoke({'run_id':run.run_id,'mode':'rca','business_context':business_context,'actor':platform.auth.subject},
            {'configurable':{'thread_id':run.run_id}},context=Execution(platform.backend,owned,policy),durability='sync')
    await expire_lease(platform,owned.guard)
    async with platform.backend.create_uow().begin_write() as uow: await uow.execute(update(s.run_jobs).values(attempt=3))
    reconciler=Reconciler(platform.backend,registry)
    reconciler.connector_policy=policy
    decision=await reconciler.recover(run.run_id)
    assert decision.action=='waiting'
    assert (await platform.get_run(run.run_id)).status=='waiting_input'
    assert (await row(platform,s.run_jobs,job_id=owned.guard.job_id))['attempt']==3

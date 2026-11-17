"""Closed durable lifecycle protocols and crash-safe projection-only ownership."""
import pytest
from sqlalchemy import select
from workbench_agent.persistence import schema as s
from workbench_agent.jobs.claim import JobService
from workbench_agent.jobs.leases import LeaseService
from workbench_agent.jobs.worker import Worker,ExecutorRegistry
from support.jobs import CAPS,claim,expire_lease,row,ReleasingProjector

async def test_projection_intent_survives_reconciler_crash_without_graph_replay(platform):
    calls=[]
    class Executor:
        async def execute(self,execution):
            calls.append('execute')
            raise ValueError('uncertain graph failure')
    class Reconciler:
        async def reconcile(self,uow,claim):
            raise AssertionError('test crashes owner before invoking reconciliation')
    registry=ExecutorRegistry();registry.register(CAPS.graph_versions[0],Executor(),ReleasingProjector(platform.backend))
    run=await platform.create_run()
    await Worker(platform.backend,'executor',registry).run_once()
    registry.reconciler=Reconciler()
    recovery=await JobService(platform.backend).claim(registry.capabilities(),'crashed-reconciler')
    assert recovery.projection_only and recovery.segment_attempt==1
    await expire_lease(platform,recovery.guard)
    normal=ExecutorRegistry();normal.register(CAPS.graph_versions[0],Executor(),ReleasingProjector(platform.backend))
    assert await JobService(platform.backend).claim(normal.capabilities(),'normal-worker') is None
    takeover=await JobService(platform.backend).claim(registry.capabilities(),'reconciler-2')
    assert takeover.projection_only and takeover.segment_attempt==1
    assert takeover.reconciliation_reason=='EXECUTOR_FAILED'
    assert (await row(platform,s.runs,run_id=run.run_id))['recovery_phase']=='projection'
    assert (await platform.get_run(run.run_id)).attempt==1
    assert calls==['execute']

async def test_successful_claim_has_leased_job_and_running_run(platform):
    run=await platform.create_run();owned=await claim(platform)
    assert (await row(platform,s.run_jobs,job_id=owned.guard.job_id))['status']=='leased'
    assert (await row(platform,s.runs,run_id=run.run_id))['status']=='running'
    assert await LeaseService(platform.backend).renew(owned.guard)

@pytest.mark.parametrize('status',['waiting_input','waiting_approval'])
async def test_waiting_kinds_release_execution_keep_thread_and_use_canonical_event(platform,status):
    run=await platform.create_run();owned=await claim(platform)
    await LeaseService(platform.backend).release(owned.guard,status=status)
    assert (await row(platform,s.runs,run_id=run.run_id))['status']==status
    assert (await row(platform,s.threads,thread_id=run.thread_id))['active_run_id']==run.run_id
    assert (await row(platform,s.run_jobs,job_id=owned.guard.job_id))['status']=='done'
    assert await row(platform,s.execution_leases,thread_id=run.thread_id) is None
    assert (await row(platform,s.teams,team_id=platform.auth.team_id))['active_executions']==0
    async with platform.backend.create_uow().begin_read() as uow:
        event=(await uow.execute(select(s.run_events).where(s.run_events.c.run_id==run.run_id).order_by(s.run_events.c.seq.desc()).limit(1))).mappings().one()
    import json
    assert event['type']=='run.status_changed'
    assert json.loads(event['payload'])['status']==status

@pytest.mark.parametrize('status,job_status',[
    ('succeeded','done'),('partial','done'),('failed','dead'),('cancelled','done'),('timed_out','dead'),('expired','done')])
async def test_every_terminal_releases_all_slots_and_uses_status_event(platform,status,job_status):
    run=await platform.create_run();owned=await claim(platform)
    await LeaseService(platform.backend).release(owned.guard,status=status)
    assert (await row(platform,s.runs,run_id=run.run_id))['status']==status
    assert (await row(platform,s.runs,run_id=run.run_id))['recovery_phase'] is None
    assert (await row(platform,s.threads,thread_id=run.thread_id))['active_run_id'] is None
    assert (await row(platform,s.teams,team_id=platform.auth.team_id))['active_executions']==0
    assert await row(platform,s.execution_leases,thread_id=run.thread_id) is None
    assert (await row(platform,s.run_jobs,job_id=owned.guard.job_id))['status']==job_status
    async with platform.backend.create_uow().begin_read() as uow:
        event=(await uow.execute(select(s.run_events).where(s.run_events.c.run_id==run.run_id).order_by(s.run_events.c.seq.desc()).limit(1))).mappings().one()
    import json
    payload=json.loads(event['payload'])
    assert event['type']=='run.status_changed'
    assert payload['status']==status and payload['revision']==3

async def test_recovery_uses_queued_retry_wait_and_persistent_phase(platform):
    run=await platform.create_run();owned=await claim(platform)
    await LeaseService(platform.backend).release(owned.guard,status='queued',job_status='retry_wait',
        recovery_phase='projection',error_code='PROJECTION_FAILED')
    durable=await row(platform,s.runs,run_id=run.run_id)
    assert durable['status']=='queued' and durable['recovery_phase']=='projection'
    assert (await row(platform,s.run_jobs,job_id=owned.guard.job_id))['status']=='retry_wait'
    async with platform.backend.create_uow().begin_read() as uow:
        event=(await uow.execute(select(s.run_events).where(s.run_events.c.run_id==run.run_id).order_by(s.run_events.c.seq.desc()).limit(1))).mappings().one()
    import json
    payload=json.loads(event['payload'])
    assert event['type']=='run.recovery_scheduled'
    assert payload['reason']=='PROJECTION_FAILED' and payload['generation']==1 and payload['attempt']==1

@pytest.mark.parametrize('invalid',['waiting','recovering','reconciliation_required','retry_wait'])
async def test_release_rejects_non_protocol_run_status(platform,invalid):
    await platform.create_run();owned=await claim(platform)
    with pytest.raises(ValueError):
        await LeaseService(platform.backend).release(owned.guard,status=invalid)

async def test_requeue_cannot_clear_projection_intent_and_safe_waiting_projection_does(platform):
    from workbench_agent.domain.guards import WorkerCapabilities
    run=await platform.create_run();owned=await claim(platform)
    leases=LeaseService(platform.backend)
    await leases.release(owned.guard,status='queued',recovery_phase='projection',error_code='PROJECTION_FAILED')
    projecting=await claim(platform)
    assert projecting.projection_only
    await leases.release(projecting.guard,status='queued',job_status='capability_blocked')
    normal=WorkerCapabilities(graph_versions=CAPS.graph_versions)
    assert await JobService(platform.backend).claim(normal,'normal') is None
    assert (await row(platform,s.runs,run_id=run.run_id))['recovery_phase']=='projection'
    assert (await row(platform,s.runs,run_id=run.run_id))['error_code']=='PROJECTION_FAILED'
    projecting=await claim(platform)
    assert projecting.projection_only and projecting.segment_attempt==1
    await leases.release(projecting.guard,status='waiting_input')
    assert (await row(platform,s.runs,run_id=run.run_id))['recovery_phase'] is None

async def test_finalizing_takeover_uses_projection_preflight_and_never_executor(platform):
    from sqlalchemy import update
    calls=[]
    class Executor:
        async def execute(self,execution):
            calls.append('unexpected-graph')
    class Reconciler:
        async def reconcile(self,uow,owned):
            calls.append('projection')
            assert (await platform.backend.runs.one(uow,'runs',run_id=owned.guard.run_id))['status']=='finalizing'
            await platform.backend.jobs.release(uow,owned.guard,status='partial')
    registry=ExecutorRegistry();registry.register(CAPS.graph_versions[0],Executor());registry.reconciler=Reconciler()
    run=await platform.create_run();owned=await claim(platform)
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(update(s.runs).where(s.runs.c.run_id==run.run_id).values(status='finalizing'))
    await expire_lease(platform,owned.guard)
    assert await Worker(platform.backend,'reconciler',registry).run_once()=='projected'
    assert calls==['projection']
    assert (await platform.get_run(run.run_id)).status=='partial'
    assert (await platform.get_run(run.run_id)).attempt==1

async def test_ordinary_operation_cannot_start_after_finalizing(platform):
    from sqlalchemy import update
    from workbench_agent.jobs.worker import Execution
    from workbench_agent.persistence.errors import StaleExecution
    run=await platform.create_run();owned=await claim(platform)
    execution=Execution(platform.backend,owned)
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(update(s.runs).where(s.runs.c.run_id==run.run_id).values(status='finalizing'))
    with pytest.raises(StaleExecution):
        await execution.check_operation()
    await execution.check_projection()

@pytest.mark.parametrize('caller_phase',[None,'running'])
async def test_finalizing_requeue_preserves_projection_intent_without_caller_hint(platform,caller_phase):
    from sqlalchemy import update
    from workbench_agent.domain.guards import WorkerCapabilities
    run=await platform.create_run();owned=await claim(platform)
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(update(s.runs).where(s.runs.c.run_id==run.run_id).values(status='finalizing',recovery_phase=None))
    await LeaseService(platform.backend).release(owned.guard,status='queued',job_status='retry_wait',recovery_phase=caller_phase)
    durable=await row(platform,s.runs,run_id=run.run_id)
    assert durable['status']=='queued' and durable['recovery_phase']=='finalizing'
    assert await JobService(platform.backend).claim(WorkerCapabilities(graph_versions=CAPS.graph_versions),'normal') is None
    reconciler=await claim(platform,'reconciler')
    assert reconciler.projection_only and reconciler.segment_attempt==1
    assert (await platform.get_run(run.run_id)).attempt==1

async def test_cancel_after_durable_finalizing_before_executor_return_never_replays_graph(platform):
    import asyncio
    from sqlalchemy import update
    calls=[];finalizing=asyncio.Event()
    class Executor:
        async def execute(self,execution):
            calls.append('execute')
            async with platform.backend.create_uow().begin_write() as uow:
                await platform.backend.lease_guard.require(uow,execution.guard,{'running'})
                await uow.execute(update(s.runs).where(s.runs.c.run_id==execution.guard.run_id).values(status='finalizing'))
            finalizing.set()
            await asyncio.Event().wait()  # Cancellation before the executor returns.
    class Reconciler:
        async def reconcile(self,uow,owned):
            calls.append('reconcile')
            assert owned.projection_only and owned.segment_attempt==1
            await platform.backend.jobs.release(uow,owned.guard,status='succeeded')
    registry=ExecutorRegistry();registry.register(CAPS.graph_versions[0],Executor(),ReleasingProjector(platform.backend))
    run=await platform.create_run();worker=Worker(platform.backend,'worker',registry)
    task=asyncio.create_task(worker.run_once())
    await asyncio.wait_for(finalizing.wait(),2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await row(platform,s.runs,run_id=run.run_id))['recovery_phase']=='finalizing'
    await worker.run_once()
    assert calls==['execute']
    registry.reconciler=Reconciler()
    assert await worker.run_once()=='projected'
    assert calls==['execute','reconcile']
    assert (await platform.get_run(run.run_id)).attempt==1

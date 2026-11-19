"""Review regressions exercise durable ownership and real worker scheduling."""
import asyncio
import pytest
from uuid import uuid4
from sqlalchemy import select,update
from workbench_agent.persistence import schema as s
from workbench_agent.domain.guards import WorkerCapabilities
from workbench_agent.jobs.claim import JobService
from workbench_agent.jobs.leases import LeaseService
from workbench_agent.jobs.worker import Worker,ExecutorRegistry,InfrastructureFailure
from support.jobs import CAPS,claim,row,ReleasingProjector

async def test_unsupported_backlog_progresses_and_changed_capability_reconsiders(platform):
    unsupported=await platform.create_run('unsupported')
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(update(s.run_jobs).where(s.run_jobs.c.run_id==unsupported.run_id).values(kind='recover',capabilities='["gpu"]'))
        for generation in range(2,66):
            await platform.backend.runs.insert(uow,'run_jobs',dict(team_id=platform.auth.team_id,job_id=str(uuid4()),run_id=unsupported.run_id,
                generation=generation,kind='recover',status='ready',available_at=0,capabilities='["gpu"]',created_at=0))
    online=await platform.create_run('online')
    service=JobService(platform.backend)
    owned=None
    for _ in range(3):
        owned=await service.claim(CAPS,'worker')
        if owned:
            break
    assert owned is not None and owned.guard.run_id==online.run_id
    assert (await row(platform,s.runs,run_id=unsupported.run_id))['run_epoch']==0
    await LeaseService(platform.backend).release(owned.guard,status='succeeded')
    restored=await service.claim(WorkerCapabilities(graph_ids=CAPS.graph_ids,features=frozenset({'gpu'})),'worker')
    assert restored is not None and restored.guard.run_id==unsupported.run_id
    assert restored.guard.run_epoch==1

async def test_unexpected_executor_failure_is_not_automatic_external_replay(platform):
    calls=[]
    class BrokenExecutor:
        async def execute(self,execution):
            calls.append('external-effect-started')
            raise ValueError('unexpected execution failure')
    registry=ExecutorRegistry();registry.register(CAPS.graph_ids[0],BrokenExecutor(),ReleasingProjector(platform.backend))
    run=await platform.create_run()
    worker=Worker(platform.backend,'worker',registry)
    for _ in range(4):
        await worker.run_once()
    assert calls==['external-effect-started']
    assert (await row(platform,s.run_jobs,run_id=run.run_id))['status']=='capability_blocked'
    assert (await row(platform,s.runs,run_id=run.run_id))['recovery_phase']=='projection'
    assert (await row(platform,s.runs,run_id=run.run_id))['error_code']=='EXECUTOR_FAILED'
    assert (await row(platform,s.teams,team_id=platform.auth.team_id))['active_executions']==0

@pytest.mark.parametrize('projection_error',[ValueError,InfrastructureFailure])
async def test_projection_failure_reconciles_without_rerunning_executor(platform,projection_error):
    calls=[]
    class Executor:
        async def execute(self,execution):
            calls.append('execute')
            async with platform.backend.create_uow().begin_write() as uow:
                await platform.backend.execution_events.append_execution(uow,execution.guard,'phase.changed',{'phase':'checkpoint_available','reference':'saved-progress'})
            return 'saved-progress'
    class BrokenProjector:
        async def project(self,uow,claim,result):
            calls.append('project')
            raise projection_error('projection failed')
    class Reconciler:
        async def reconcile(self,uow,claim):
            calls.append('reconcile')
            assert claim.projection_only
            assert claim.reconciliation_reason=='PROJECTION_FAILED'
            await platform.backend.jobs.release(uow,claim.guard,status='waiting_input')
    registry=ExecutorRegistry();registry.register(CAPS.graph_ids[0],Executor(),BrokenProjector())
    run=await platform.create_run()
    worker=Worker(platform.backend,'worker',registry)
    await worker.run_once()
    await worker.run_once()  # No reconciler: must remain parked, never rerun graph.
    registry.reconciler=Reconciler()
    await worker.run_once()
    assert calls==['execute','project','reconcile']
    assert (await platform.get_run(run.run_id)).attempt==1
    assert (await row(platform,s.runs,run_id=run.run_id))['status']=='waiting_input'
    assert (await row(platform,s.threads,thread_id=run.thread_id))['active_run_id']==run.run_id

async def test_event_attempt_excludes_projection_only_epoch_and_counts_human_resume(platform):
    run=await platform.create_run()
    leases=LeaseService(platform.backend)
    for _ in range(3):
        owned=await claim(platform,'worker')
        await leases.release(owned.guard,status='queued',job_status='retry_wait')
    projection=await claim(platform,'reconciler')
    assert projection.projection_only and projection.guard.run_epoch==4
    await leases.release(projection.guard,status='waiting_input')
    async with platform.backend.create_uow().begin_write() as uow:
        await platform.backend.runs.insert(uow,'run_jobs',dict(team_id=platform.auth.team_id,job_id=str(uuid4()),run_id=run.run_id,
            generation=2,kind='resume',status='ready',available_at=0,capabilities='[]'))
    resumed=await claim(platform,'worker')
    assert not resumed.projection_only and resumed.guard.run_epoch==5
    assert resumed.segment_attempt==1
    await leases.release(resumed.guard,status='succeeded')
    async with platform.backend.create_uow().begin_read() as uow:
        attempts=list((await uow.execute(select(s.run_events.c.attempt).where(s.run_events.c.run_id==run.run_id).order_by(s.run_events.c.seq))).scalars())
    assert attempts==[0,1,1,2,2,3,3,3,4,4]
    assert (await platform.get_run(run.run_id)).attempt==4

async def test_long_admin_work_does_not_consume_either_run_slot(platform):
    from workbench_agent.domain.admin_jobs import AdminAuth
    from workbench_agent.jobs.admin import AdminJobService
    admin_started,admin_release=asyncio.Event(),asyncio.Event()
    run_release=asyncio.Event(); started=[]; both_started=asyncio.Event()
    class Executor:
        async def execute(self,execution):
            started.append(execution.guard.run_id)
            if len(started)==2:
                both_started.set()
            await run_release.wait()
    async def admin(claim):
        admin_started.set()
        await admin_release.wait()
        return 'durable-probe-result'
    registry=ExecutorRegistry();registry.register(CAPS.graph_ids[0],Executor(),ReleasingProjector(platform.backend));registry.register_admin('model_probe',admin)
    auth=AdminAuth(platform.auth.team_id,platform.agent_id,platform.auth.subject,True)
    admin_job=await AdminJobService(platform.backend).submit(auth,'model_probe',{},'probe')
    worker=Worker(platform.backend,'worker',registry,scan_interval=.01)
    task=asyncio.create_task(worker.run())
    try:
        await asyncio.wait_for(admin_started.wait(),2)
        a=await platform.create_run('a');b=await platform.create_run('b')
        await asyncio.wait_for(both_started.wait(),2)
        assert set(started)=={a.run_id,b.run_id}
        assert not admin_release.is_set()
        assert (await row(platform,s.teams,team_id=platform.auth.team_id))['active_executions']==2
    finally:
        worker.stop();admin_release.set();run_release.set()
        await asyncio.wait_for(task,3)

    assert (await AdminJobService(platform.backend).get(auth,admin_job.job_id)).status=='succeeded'

async def test_admin_admission_yields_to_ready_run_without_claiming_it(platform):
    from workbench_agent.domain.admin_jobs import AdminAuth
    from workbench_agent.jobs.admin import AdminJobService
    calls=[]
    class Executor:
        async def execute(self,execution):
            calls.append('run')
    async def admin(claim):
        calls.append('admin')
        return 'result'
    registry=ExecutorRegistry();registry.register(CAPS.graph_ids[0],Executor(),ReleasingProjector(platform.backend));registry.register_admin('model_probe',admin)
    run=await platform.create_run()
    auth=AdminAuth(platform.auth.team_id,platform.agent_id,platform.auth.subject,True)
    await AdminJobService(platform.backend).submit(auth,'model_probe',{},'probe')
    worker=Worker(platform.backend,'worker',registry)
    assert await worker.run_admin_once()=='run_priority'
    assert calls==[]
    assert (await row(platform,s.runs,run_id=run.run_id))['run_epoch']==0
    assert await worker.run_once()=='projected'
    assert await worker.run_admin_once()=='admin_completed'
    assert calls==['run','admin']

async def test_cancel_after_executor_return_parks_projection_without_graph_replay(platform):
    entered=asyncio.Event();calls=[]
    class Executor:
        async def execute(self,execution):
            calls.append('execute')
            return 'persisted-checkpoint'
    class Projector:
        async def project(self,uow,claim,result):
            entered.set()
            await asyncio.Event().wait()
    registry=ExecutorRegistry();registry.register(CAPS.graph_ids[0],Executor(),Projector())
    run=await platform.create_run()
    worker=Worker(platform.backend,'worker',registry)
    task=asyncio.create_task(worker.run_once())
    await asyncio.wait_for(entered.wait(),2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await row(platform,s.run_jobs,run_id=run.run_id))['status']=='retry_wait'
    assert (await row(platform,s.runs,run_id=run.run_id))['error_code']=='PROJECTION_CANCELLED'
    await worker.run_once()
    assert calls==['execute']

@pytest.mark.parametrize('cancel_state',['cancel_requested','cancelled'])
async def test_cooperative_shutdown_never_resets_explicit_user_cancellation(platform,cancel_state):
    entered=asyncio.Event()
    class Executor:
        async def execute(self,execution):
            entered.set()
            await asyncio.Event().wait()
    registry=ExecutorRegistry();registry.register(CAPS.graph_ids[0],Executor(),ReleasingProjector(platform.backend))
    run=await platform.create_run()
    worker=Worker(platform.backend,'worker',registry)
    task=asyncio.create_task(worker.run_once())
    await asyncio.wait_for(entered.wait(),2)
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(update(s.runs).where(s.runs.c.run_id==run.run_id).values(status=cancel_state))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await row(platform,s.runs,run_id=run.run_id))['status']=='cancelled'
    if cancel_state=='cancel_requested':
        assert await row(platform,s.execution_leases,owner_run_id=run.run_id) is None
        assert (await row(platform,s.threads,thread_id=run.thread_id))['active_run_id'] is None
    assert await worker.run_once()=='idle'

async def test_cli_once_still_executes_supported_admin_outside_business_slots(platform):
    from workbench_agent.cli.worker import run_worker
    from workbench_agent.domain.admin_jobs import AdminAuth
    from workbench_agent.jobs.admin import AdminJobService
    registry=ExecutorRegistry()
    async def admin(claim):
        return 'durable-admin-result'
    registry.register_admin('model_probe',admin)
    auth=AdminAuth(platform.auth.team_id,platform.agent_id,platform.auth.subject,True)
    job=await AdminJobService(platform.backend).submit(auth,'model_probe',{},'probe')
    assert await run_worker(platform.settings,once=True,registry=registry)=={'ready':True,'status':'admin_completed'}
    assert (await AdminJobService(platform.backend).get(auth,job.job_id)).status=='succeeded'

async def test_missing_graph_capability_never_erases_projection_only_recovery(platform):
    await platform.create_run()
    owned=await claim(platform,'worker')
    await LeaseService(platform.backend).release(owned.guard,status='queued',job_status='retry_wait',recovery_phase='projection',error_code='PROJECTION_FAILED')
    service=JobService(platform.backend)
    assert await service.claim(WorkerCapabilities(can_reconcile=True),'missing-graph') is None
    job=await row(platform,s.run_jobs,job_id=owned.guard.job_id)
    assert job['status']=='capability_blocked'
    assert (await row(platform,s.runs,run_id=owned.guard.run_id))['recovery_phase']=='projection'
    restored=await service.claim(WorkerCapabilities(graph_ids=CAPS.graph_ids,projection_graph_ids=CAPS.graph_ids,can_reconcile=True),'reconciler')
    assert restored.projection_only and restored.reconciliation_reason=='PROJECTION_FAILED'
    assert restored.segment_attempt==1
    assert (await platform.get_run(owned.guard.run_id)).attempt==1

async def test_reconciler_capability_does_not_admit_fresh_execution_without_projector(platform):
    calls=[]
    class Executor:
        async def execute(self,execution):
            calls.append('execute')
    class Reconciler:
        async def reconcile(self,uow,claim):
            calls.append('reconcile')
            await platform.backend.jobs.release(uow,claim.guard,status='waiting_input')
    registry=ExecutorRegistry();registry.register(CAPS.graph_ids[0],Executor());registry.reconciler=Reconciler()
    fresh=await platform.create_run('fresh')
    worker=Worker(platform.backend,'worker',registry)
    assert await worker.run_once()=='capability_blocked'
    assert calls==[]
    assert (await row(platform,s.runs,run_id=fresh.run_id))['run_epoch']==0
    recovering=await platform.create_run('recovering')
    # Test support obtains ownership independently of the incomplete registry.
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(update(s.run_jobs).where(s.run_jobs.c.run_id==fresh.run_id).values(available_at=2**62))
    owned=await claim(platform,'fixture')
    assert owned.guard.run_id==recovering.run_id
    await LeaseService(platform.backend).release(owned.guard,status='queued',job_status='retry_wait',recovery_phase='projection',error_code='PROJECTION_FAILED')
    assert await worker.run_once()=='projected'
    assert calls==['reconcile']
    assert (await platform.get_run(recovering.run_id)).attempt==1

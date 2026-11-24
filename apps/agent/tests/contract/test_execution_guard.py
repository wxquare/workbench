"""Real SQLite contracts: removing any ownership/quota CAS breaks these tests."""

import asyncio
from dataclasses import replace
import pytest
from sqlalchemy import select, update
from workbench_agent.persistence import schema as s
from workbench_agent.persistence.errors import StaleExecution
from support.jobs import claim, expire_lease, row, ReleasingProjector


async def test_claim_is_atomic_and_failed_candidate_does_not_bump_epoch(platform):
    first = await platform.create_run()
    second = await platform.create_run(key='second', thread_id=first.thread_id)
    owner = await claim(platform, 'a')
    assert owner.guard.run_id == first.run_id
    assert owner.guard.run_epoch == 1
    assert await claim(platform, 'b') is None
    assert (await row(platform, s.runs, run_id=second.run_id))['run_epoch'] == 0
    assert (await row(platform, s.teams, team_id=platform.auth.team_id))['active_executions'] == 1
    assert (await row(platform, s.threads, thread_id=first.thread_id))[
        'active_run_id'
    ] == first.run_id
    job = await row(platform, s.run_jobs, run_id=second.run_id)
    assert job['available_at'] > job['created_at']


async def test_old_owner_cannot_append_after_takeover(platform):
    from workbench_agent.jobs.leases import LeaseService

    await platform.create_run()
    old = await claim(platform, 'a')
    await expire_lease(platform, old.guard)
    new = await claim(platform, 'b')
    assert new.guard.run_epoch == 2
    service = LeaseService(platform.backend)
    with pytest.raises(StaleExecution):
        async with platform.backend.create_uow().begin_write() as uow:
            await platform.backend.execution_events.append_execution(
                uow, old.guard, 'phase.changed', {'phase': 'late'}
            )
    assert await service.renew(old.guard) is False
    assert await service.renew(new.guard) is True
    assert (await row(platform, s.teams, team_id=platform.auth.team_id))['active_executions'] == 1


async def test_guard_identity_status_and_expiry_all_required(platform):
    await platform.create_run()
    owned = await claim(platform, 'a')
    for field, value in [
        ('worker_id', 'other'),
        ('job_id', 'other'),
        ('run_id', 'other'),
        ('thread_id', 'other'),
        ('run_epoch', 99),
    ]:
        with pytest.raises(StaleExecution):
            async with platform.backend.create_uow().begin_write() as uow:
                await platform.backend.lease_guard.require(
                    uow, replace(owned.guard, **{field: value}), {'running'}
                )
    with pytest.raises(StaleExecution):
        async with platform.backend.create_uow().begin_write() as uow:
            await platform.backend.lease_guard.require(uow, owned.guard, {'waiting'})
    await expire_lease(platform, owned.guard)
    with pytest.raises(StaleExecution):
        async with platform.backend.create_uow().begin_write() as uow:
            await platform.backend.lease_guard.require(uow, owned.guard, {'running'})


async def test_event_and_guard_rollback_together_and_allocate_monotonic_seq(platform):
    await platform.create_run()
    owned = await claim(platform, 'a')
    before = await row(platform, s.execution_leases, thread_id=owned.guard.thread_id)
    with pytest.raises(RuntimeError):
        async with platform.backend.create_uow().begin_write() as uow:
            await platform.backend.execution_events.append_execution(
                uow, owned.guard, 'phase.changed', {}
            )
            raise RuntimeError('rollback')
    after = await row(platform, s.execution_leases, thread_id=owned.guard.thread_id)
    assert after['guard_revision'] == before['guard_revision']
    async with platform.backend.create_uow().begin_write() as uow:
        a = await platform.backend.execution_events.append_execution(
            uow, owned.guard, 'phase.changed', {}
        )
        b = await platform.backend.execution_events.append_execution(
            uow, owned.guard, 'phase.changed', {}
        )
    assert (a, b) == (3, 4)  # run.accepted + run.started precede these writes.


async def test_waiting_releases_execution_but_retains_thread(platform):
    from workbench_agent.jobs.leases import LeaseService

    run = await platform.create_run()
    owned = await claim(platform, 'a')
    await LeaseService(platform.backend).release(owned.guard, status='waiting_input')
    assert (await row(platform, s.threads, thread_id=run.thread_id))['active_run_id'] == run.run_id
    assert (await row(platform, s.teams, team_id=platform.auth.team_id))['active_executions'] == 0
    assert await row(platform, s.execution_leases, thread_id=run.thread_id) is None
    await platform.create_run(key='blocked', thread_id=run.thread_id)
    assert await claim(platform, 'b') is None


async def test_team_quota_and_unsupported_capability_do_not_claim(platform):
    from workbench_agent.jobs.claim import JobService
    from workbench_agent.domain.guards import WorkerCapabilities

    await platform.create_run(key='1')
    await platform.create_run(key='2')
    await platform.create_run(key='3')
    assert await JobService(platform.backend).claim(WorkerCapabilities(), 'none') is None
    assert await claim(platform, 'a')
    assert await claim(platform, 'b')
    assert await claim(platform, 'c') is None
    async with platform.backend.create_uow().begin_read() as uow:
        epochs = list((await uow.execute(select(s.runs.c.run_epoch))).scalars())
    assert sorted(epochs) == [0, 1, 1]


async def test_worker_renewal_failure_cancels_attempt_without_finalizing(platform):
    from workbench_agent.jobs.worker import Worker, ExecutorRegistry
    from support.jobs import CAPS

    started, cancelled = asyncio.Event(), asyncio.Event()

    class Executor:
        async def execute(self, execution):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    registry = ExecutorRegistry()
    registry.register(CAPS.graph_ids[0], Executor(), ReleasingProjector(platform.backend))
    run = await platform.create_run()
    worker = Worker(platform.backend, 'a', registry, renew_interval=0.01)
    task = asyncio.create_task(worker.run_once())
    await asyncio.wait_for(started.wait(), 2)
    _owned = await row(platform, s.execution_leases, thread_id=run.thread_id)
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(
            update(s.execution_leases)
            .where(s.execution_leases.c.thread_id == run.thread_id)
            .values(expires_at=0)
        )
    await asyncio.wait_for(task, 2)
    assert cancelled.is_set()
    assert (await row(platform, s.runs, run_id=run.run_id))['status'] == 'running'


async def test_claim_rollback_leaves_all_ownership_unchanged(platform):
    from support.jobs import CAPS

    run = await platform.create_run()
    with pytest.raises(RuntimeError):
        async with platform.backend.create_uow().begin_write() as uow:
            candidates = await platform.backend.jobs.candidates(uow)
            owned, _ = await platform.backend.jobs.claim(uow, CAPS, 'a', candidates)
            assert owned
            raise RuntimeError('abort claim')
    assert (await row(platform, s.runs, run_id=run.run_id))['run_epoch'] == 0
    assert (await row(platform, s.teams, team_id=platform.auth.team_id))['active_executions'] == 0
    assert (await row(platform, s.threads, thread_id=run.thread_id))['active_run_id'] is None
    assert await row(platform, s.execution_leases, thread_id=run.thread_id) is None
    assert (await row(platform, s.run_jobs, run_id=run.run_id))['status'] == 'ready'


async def test_membership_revoked_before_execution_prevents_executor(platform):
    from workbench_agent.jobs.worker import Worker, ExecutorRegistry
    from support.jobs import CAPS

    called = []

    class Executor:
        async def execute(self, execution):
            called.append(execution)

    registry = ExecutorRegistry()
    registry.register(CAPS.graph_ids[0], Executor(), ReleasingProjector(platform.backend))
    run = await platform.create_run()
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(
            update(s.team_members)
            .where(s.team_members.c.team_id == platform.auth.team_id)
            .values(status='revoked')
        )
    assert await Worker(platform.backend, 'a', registry).run_once() == 'capability_blocked'
    assert called == []
    assert (await row(platform, s.runs, run_id=run.run_id))['status'] == 'queued'


async def test_recover_generation_cannot_reset_three_attempt_segment(platform):
    from uuid import uuid4
    from workbench_agent.jobs.claim import JobService
    from support.jobs import CAPS

    run = await platform.create_run()
    owned = await claim(platform, 'a')
    await expire_lease(platform, owned.guard)
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(
            update(s.run_jobs)
            .where(s.run_jobs.c.job_id == owned.guard.job_id)
            .values(attempt=3, status='done')
        )
        await platform.backend.runs.insert(
            uow,
            'run_jobs',
            dict(
                team_id=platform.auth.team_id,
                job_id=str(uuid4()),
                run_id=run.run_id,
                generation=2,
                kind='recover',
                status='ready',
                available_at=0,
                capabilities='[]',
            ),
        )
    reconciler = await JobService(platform.backend).claim(CAPS, 'b')
    assert reconciler.projection_only
    assert reconciler.segment_attempt == 3
    assert reconciler.attempt == 0
    assert reconciler.guard.run_epoch == 2


async def test_frozen_version_restored_after_publication_changes(platform):
    run = await platform.create_run()
    original = (await platform.get_run(run.run_id)).context
    await platform.publish_model('new-model')
    claimed = await claim(platform, 'a')
    assert claimed.context == original


async def test_cancel_propagates_and_releases_owned_execution(platform):
    from workbench_agent.jobs.worker import Worker, ExecutorRegistry
    from support.jobs import CAPS

    started = asyncio.Event()
    cancelled = asyncio.Event()

    class Executor:
        async def execute(self, execution):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    registry = ExecutorRegistry()
    registry.register(CAPS.graph_ids[0], Executor(), ReleasingProjector(platform.backend))
    run = await platform.create_run()
    task = asyncio.create_task(Worker(platform.backend, 'a', registry).run_once())
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()
    assert await row(platform, s.execution_leases, thread_id=run.thread_id) is None
    assert (await row(platform, s.teams, team_id=platform.auth.team_id))['active_executions'] == 0
    assert (await row(platform, s.run_jobs, run_id=run.run_id))['status'] == 'retry_wait'


async def test_projection_only_does_not_call_executor_and_guards_terminal_release(platform):
    from workbench_agent.jobs.worker import Worker, ExecutorRegistry
    from support.jobs import CAPS

    calls = []

    class Executor:
        async def execute(self, execution):
            calls.append('execute')

    class Reconciler:
        async def reconcile(self, uow, claim):
            calls.append(('reconcile', claim.segment_attempt))
            await platform.backend.jobs.release(
                uow, claim.guard, status='failed', error_code='RETRY_EXHAUSTED'
            )

    registry = ExecutorRegistry()
    registry.register(CAPS.graph_ids[0], Executor(), ReleasingProjector(platform.backend))
    registry.reconciler = Reconciler()
    run = await platform.create_run()
    owned = await claim(platform, 'a')
    await expire_lease(platform, owned.guard)
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(
            update(s.run_jobs).where(s.run_jobs.c.run_id == run.run_id).values(attempt=3)
        )
    assert await Worker(platform.backend, 'b', registry).run_once() == 'projected'
    assert calls == [('reconcile', 3)]
    assert (await row(platform, s.threads, thread_id=run.thread_id))['active_run_id'] is None
    assert (await row(platform, s.teams, team_id=platform.auth.team_id))['active_executions'] == 0
    assert (await row(platform, s.runs, run_id=run.run_id))['error_code'] == 'RETRY_EXHAUSTED'


async def test_priority_and_team_rotation_use_real_candidates(platform):
    from workbench_agent.application.runs import RunService
    from workbench_agent.domain.runs import CreateRun
    from support.runs import make_run_payload
    from workbench_agent.jobs.claim import JobService
    from workbench_agent.jobs.leases import LeaseService
    from support.jobs import CAPS

    async with platform.backend.create_uow().begin_read() as uow:
        other_agent = await platform.backend.configuration.one(
            uow, 'agents', team_id=platform.other_auth.team_id
        )
    a1 = await platform.create_run('a1')
    a2 = await platform.create_run('a2')
    b1 = await RunService(platform.backend).create(
        platform.other_auth,
        CreateRun.model_validate(make_run_payload(other_agent['agent_id'])),
        'b1',
    )
    service = JobService(platform.backend)
    first = await service.claim(CAPS, 'a')
    assert first.guard.run_id == a1.run_id
    await LeaseService(platform.backend).release(first.guard, status='succeeded')
    second = await service.claim(CAPS, 'a')
    assert second.guard.run_id == b1.run_id
    await LeaseService(platform.backend).release(second.guard, status='succeeded')
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(
            update(s.run_jobs).where(s.run_jobs.c.run_id == a2.run_id).values(kind='schedule')
        )
    urgent = await platform.create_run('urgent')
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(
            update(s.run_jobs).where(s.run_jobs.c.run_id == urgent.run_id).values(kind='resume')
        )
    assert (await service.claim(CAPS, 'a')).guard.run_id == urgent.run_id


async def test_caught_stale_guard_still_aborts_transaction(platform):
    from workbench_agent.persistence.errors import StorageError

    run = await platform.create_run()
    owned = await claim(platform, 'a')
    with pytest.raises(RuntimeError, match='transaction is closed or failed'):
        async with platform.backend.create_uow().begin_write() as uow:
            try:
                await platform.backend.lease_guard.require(
                    uow, replace(owned.guard, worker_id='wrong'), {'running'}
                )
            except StaleExecution:
                pass
            await uow.execute(
                update(s.runs).where(s.runs.c.run_id == run.run_id).values(phase='stale-write')
            )
    assert (await row(platform, s.runs, run_id=run.run_id))['phase'] is None


async def test_missing_projector_never_reexecutes_completed_executor(platform):
    from workbench_agent.jobs.worker import Worker, ExecutorRegistry
    from support.jobs import CAPS

    calls = []

    class Executor:
        async def execute(self, execution):
            calls.append('ran')

    registry = ExecutorRegistry()
    registry.register(CAPS.graph_ids[0], Executor())
    await platform.create_run()
    assert await Worker(platform.backend, 'a', registry).run_once() == 'capability_blocked'
    assert calls == []


async def test_bounded_scan_preserves_resume_priority_over_old_schedule_backlog(platform):
    from uuid import uuid4
    from support.jobs import CAPS
    from workbench_agent.jobs.claim import JobService

    old = await platform.create_run('old')
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(
            update(s.run_jobs).where(s.run_jobs.c.run_id == old.run_id).values(kind='schedule')
        )
        for generation in range(2, 66):
            await platform.backend.runs.insert(
                uow,
                'run_jobs',
                dict(
                    team_id=platform.auth.team_id,
                    job_id=str(uuid4()),
                    run_id=old.run_id,
                    generation=generation,
                    kind='schedule',
                    status='ready',
                    available_at=0,
                    capabilities='[]',
                    created_at=0,
                ),
            )
    urgent = await platform.create_run('urgent')
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(
            update(s.run_jobs).where(s.run_jobs.c.run_id == urgent.run_id).values(kind='resume')
        )
    assert (await JobService(platform.backend).claim(CAPS, 'a')).guard.run_id == urgent.run_id


async def test_three_infrastructure_attempts_then_projection_only_with_durable_backoff(platform):
    from workbench_agent.jobs.worker import Worker, ExecutorRegistry, InfrastructureFailure
    from support.jobs import CAPS

    calls = []

    class Executor:
        async def execute(self, execution):
            calls.append(execution.claim.segment_attempt)
            raise InfrastructureFailure()

    class Reconciler:
        async def reconcile(self, uow, claim):
            calls.append('reconcile')
            await platform.backend.jobs.release(
                uow, claim.guard, status='failed', error_code='RETRY_EXHAUSTED'
            )

    registry = ExecutorRegistry()
    registry.register(CAPS.graph_ids[0], Executor(), ReleasingProjector(platform.backend))
    registry.reconciler = Reconciler()
    run = await platform.create_run()
    worker = Worker(platform.backend, 'a', registry, jitter=lambda: 0)
    for expected_delay in (2000, 10000, 0):
        assert await worker.run_once() == 'retry_wait'
        job = await row(platform, s.run_jobs, run_id=run.run_id)
        assert job['available_at'] - job['updated_at'] == expected_delay
        async with platform.backend.create_uow().begin_write() as uow:
            await uow.execute(
                update(s.run_jobs).where(s.run_jobs.c.run_id == run.run_id).values(available_at=0)
            )
    assert await worker.run_once() == 'projected'
    assert calls == [1, 2, 3, 'reconcile']


async def test_worker_cli_with_explicit_empty_registry_reports_capability_blocked(platform):
    from workbench_agent.cli.worker import run_worker
    from workbench_agent.jobs.worker import ExecutorRegistry

    run = await platform.create_run()
    report = await run_worker(platform.settings, once=True, registry=ExecutorRegistry())
    assert report == {'ready': True, 'status': 'capability_blocked'}
    assert (await row(platform, s.runs, run_id=run.run_id))['run_epoch'] == 0


async def test_worker_admin_jobs_have_distinct_guard_and_no_thread_slot(platform):
    from workbench_agent.domain.admin_jobs import AdminAuth
    from workbench_agent.jobs.admin import AdminJobService
    from workbench_agent.jobs.worker import Worker, ExecutorRegistry

    registry = ExecutorRegistry()

    async def probe(claim):
        assert not hasattr(claim, 'run_id')
        return 'probe-result'

    registry.admin['model_probe'] = probe
    auth = AdminAuth(platform.auth.team_id, platform.agent_id, platform.auth.subject, True)
    job = await AdminJobService(platform.backend).submit(auth, 'model_probe', {}, 'probe')
    assert await Worker(platform.backend, 'a', registry).run_admin_once() == 'admin_completed'
    assert (await AdminJobService(platform.backend).get(auth, job.job_id)).status == 'succeeded'
    assert await platform.count_rows('threads') == 0
    assert await platform.count_rows('execution_leases') == 0
    assert (await row(platform, s.teams, team_id=platform.auth.team_id))['active_executions'] == 0


@pytest.mark.parametrize('surface', ['checkpoint', 'pending', 'budget', 'result', 'event'])
async def test_future_protected_stores_share_same_transaction_fence(platform, surface):
    from support.jobs import guarded_write

    run = await platform.create_run()
    old = await claim(platform, 'a')
    await expire_lease(platform, old.guard)
    new = await claim(platform, 'b')
    with pytest.raises(StaleExecution):
        await guarded_write(platform, old.guard, surface)
    assert (await row(platform, s.runs, run_id=run.run_id))['outcome_ref'] is None
    assert await platform.count_rows('graph_checkpoints') == 0
    assert await platform.count_rows('graph_pending_writes') == 0
    assert (await row(platform, s.execution_leases, thread_id=run.thread_id))['worker_id'] == 'b'
    assert new.guard.run_epoch == 2


async def test_failed_admin_executor_does_not_kill_worker_slot(platform):
    from workbench_agent.domain.admin_jobs import AdminAuth
    from workbench_agent.jobs.admin import AdminJobService
    from workbench_agent.jobs.worker import Worker, ExecutorRegistry

    registry = ExecutorRegistry()

    async def broken(claim):
        raise ValueError('failed external probe')

    registry.admin['model_probe'] = broken
    auth = AdminAuth(platform.auth.team_id, platform.agent_id, platform.auth.subject, True)
    await AdminJobService(platform.backend).submit(auth, 'model_probe', {}, 'probe')
    assert await Worker(platform.backend, 'a', registry).run_admin_once() == 'admin_blocked'


async def test_current_connector_policy_revocation_overrides_frozen_scope(platform):
    from workbench_agent.jobs.worker import Execution, DeploymentConnectorPolicy, CapabilityBlocked

    await platform.create_run()
    owned = await claim(platform, 'a')
    frozen = owned.context.model_copy(
        update={'identity_kind': 'connector', 'agent_scope': (platform.agent_id,)}
    )
    owned = replace(owned, context=frozen)
    bindings = {
        'connector': {
            'subject': frozen.subject,
            'team_id': frozen.team_id,
            'agent_ids': [platform.agent_id],
        }
    }
    execution = Execution(platform.backend, owned, DeploymentConnectorPolicy(lambda: bindings))
    await execution.check_operation()
    bindings.clear()
    with pytest.raises(CapabilityBlocked):
        await execution.check_operation()
    assert execution.claim.context.agent_scope == (platform.agent_id,)


async def test_run_view_reports_durable_claim_attempts(platform):
    from workbench_agent.jobs.leases import LeaseService

    run = await platform.create_run()
    assert (await platform.get_run(run.run_id)).attempt == 0
    owned = await claim(platform, 'a')
    assert (await platform.get_run(run.run_id)).attempt == 1
    await LeaseService(platform.backend).renew(owned.guard)
    assert (await platform.get_run(run.run_id)).attempt == 1
    await LeaseService(platform.backend).release(
        owned.guard, status='queued', job_status='retry_wait'
    )
    await claim(platform, 'a')
    assert (await platform.get_run(run.run_id)).attempt == 2


@pytest.mark.parametrize('channel', ['execution', 'control'])
@pytest.mark.parametrize('event_type', ['phase.old', 'checkpoint.available', 'run.waiting'])
async def test_event_allocator_rejects_noncanonical_new_event_types(platform, channel, event_type):
    run = await platform.create_run()
    owned = await claim(platform)
    before = (await row(platform, s.runs, run_id=run.run_id))['last_event_seq']
    with pytest.raises(ValueError, match='canonical'):
        async with platform.backend.create_uow().begin_write() as uow:
            if channel == 'execution':
                await platform.backend.execution_events.append_execution(
                    uow, owned.guard, event_type, {}
                )
            else:
                await platform.backend.execution_events.append_control(
                    uow, platform.auth.team_id, run.run_id, event_type, {}
                )
    assert (await row(platform, s.runs, run_id=run.run_id))['last_event_seq'] == before

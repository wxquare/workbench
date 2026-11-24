import pytest
from sqlalchemy import update, select
from support.jobs import claim, row, expire_lease
from support.interrupts import InterruptPlatform
from workbench_agent.jobs.worker import Execution
from workbench_agent.persistence import schema as s


@pytest.fixture(autouse=True)
def deterministic_model(platform):
    from support.model import install_test_model

    install_test_model(platform.backend)


@pytest.mark.asyncio
@pytest.mark.parametrize('attempts', [1, 3])
async def test_saved_outcome_wins_over_retry_exhaustion(platform, attempts):
    from workbench_agent.jobs.reconciler import Reconciler

    run = await platform.create_run()
    owned = await claim(platform)
    from workbench_agent.runtime.graphs.registry import foundation_worker_registry

    registry = foundation_worker_registry(platform.backend)
    await registry._executors['conversation.multi_turn'].execute(Execution(platform.backend, owned))
    await expire_lease(platform, owned.guard)
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(update(s.run_jobs).values(attempt=attempts))
    result = await Reconciler(platform.backend, registry).recover(run.run_id)
    assert result.action == 'finalized'
    assert (await platform.get_run(run.run_id)).status == 'succeeded'
    assert (await row(platform, s.run_jobs, job_id=owned.guard.job_id))['attempt'] == attempts
    assert (await Reconciler(platform.backend, registry).recover(run.run_id)).action == 'terminal'


@pytest.mark.asyncio
async def test_applied_marker_recovers_without_second_command_then_continues_outside_write(
    platform,
):
    from support.rca_recovery import RcaRecovery

    p = RcaRecovery(platform)
    run, w = await p.waiting_run()
    await p.resume(run, w)
    owned = await claim(platform)
    await p.executor.apply_resume(Execution(platform.backend, owned, p.policy), mark=False)
    await expire_lease(platform, owned.guard)
    decision = await p.registry.reconciler.recover(run.run_id)
    assert decision.action == 'continue'
    assert (await row(platform, s.resume_requests, run_id=run.run_id))['status'] == 'applied'
    assert (await row(platform, s.interrupts, interrupt_id=w['interrupt_id']))[
        'status'
    ] == 'consumed'
    async with platform.backend.create_uow().begin_read() as uow:
        jobs = (
            (
                await uow.execute(
                    select(s.run_jobs)
                    .where(s.run_jobs.c.run_id == run.run_id)
                    .order_by(s.run_jobs.c.generation)
                )
            )
            .mappings()
            .all()
        )
    assert [j['status'] for j in jobs] == ['done', 'done', 'ready']
    assert jobs[-1]['kind'] == 'recover'
    assert await p.worker.run_once() == 'projected'
    assert (await platform.get_run(run.run_id)).status == 'partial'


@pytest.mark.asyncio
async def test_no_checkpoint_exhausted_fails_without_fourth_execution(platform):
    from workbench_agent.jobs.reconciler import Reconciler
    from workbench_agent.runtime.graphs.registry import foundation_worker_registry

    run = await platform.create_run()
    owned = await claim(platform)
    await expire_lease(platform, owned.guard)
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(update(s.run_jobs).values(attempt=3))
    decision = await Reconciler(
        platform.backend, foundation_worker_registry(platform.backend)
    ).recover(run.run_id)
    assert decision.action == 'failed' and decision.reason == 'RETRY_EXHAUSTED'
    assert (await row(platform, s.run_jobs, job_id=owned.guard.job_id))['attempt'] == 3


@pytest.mark.asyncio
async def test_finalizing_without_result_fails_closed_not_continue(platform):
    from workbench_agent.jobs.reconciler import Reconciler
    from workbench_agent.runtime.graphs.registry import foundation_worker_registry

    run = await platform.create_run()
    owned = await claim(platform)
    await expire_lease(platform, owned.guard)
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(update(s.runs).values(status='finalizing', recovery_phase='finalizing'))
    decision = await Reconciler(
        platform.backend, foundation_worker_registry(platform.backend)
    ).recover(run.run_id)
    assert decision.reason == 'STATE_RECONCILIATION_REQUIRED'
    assert (await platform.get_run(run.run_id)).status == 'failed'


@pytest.mark.asyncio
async def test_cancel_requested_dead_owner_is_fenced_and_cleaned(platform):
    from workbench_agent.jobs.reconciler import Reconciler
    from workbench_agent.runtime.graphs.registry import foundation_worker_registry

    run = await platform.create_run()
    owned = await claim(platform)
    from workbench_agent.application.runs import RunService

    await RunService(platform.backend).cancel(platform.auth, run.run_id, 'cancel')
    await expire_lease(platform, owned.guard)
    decision = await Reconciler(
        platform.backend, foundation_worker_registry(platform.backend)
    ).recover(run.run_id)
    assert decision.action == 'cancelled'
    assert (await platform.get_run(run.run_id)).status == 'cancelled'


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'hook', ['after_interrupt_checkpoint', 'after_input_application', 'after_final_checkpoint']
)
async def test_real_sigkill_named_checkpoint_boundaries(platform, tmp_path, hook):
    import asyncio, time
    from support.processes import ProcessFault, ledger_count
    from support.rca_recovery import RcaRecovery
    from workbench_agent.jobs.scheduler import Scheduler

    p = RcaRecovery(platform)
    if hook == 'after_input_application':
        run, w = await p.waiting_run()
        await p.resume(run, w)
        state = None
    else:
        run, state = await p.admit(conflict=hook == 'after_interrupt_checkpoint')
    ledger = tmp_path / 'nodes.log'
    identities = {
        'v2-test': {
            'subject': platform.auth.subject,
            'team_id': platform.auth.team_id,
            'agent_ids': [platform.agent_id],
        }
    }
    boundary = await ProcessFault(
        platform.settings.storage.database_url.get_secret_value(), hook, ledger, state, identities
    ).crash()
    start = time.monotonic()
    if hook != 'after_interrupt_checkpoint':
        await expire_lease(platform, boundary['guard'])
    # This first case uses the actual 45s persisted lease, 5s scheduler scan and
    # real process death. Other cases shorten only post-kill expiry for speed.
    scheduler = Scheduler(platform.backend, p.registry, connector_policy=p.policy)
    while time.monotonic() - start < 90:
        await scheduler.tick()
        await p.worker.run_once()
        view = await platform.get_run(run.run_id)
        if view.status in ('waiting_input', 'partial'):
            break
        await asyncio.sleep(5 if hook == 'after_interrupt_checkpoint' else 0.05)
    elapsed = time.monotonic() - start
    print(
        f'RECOVERY hook={hook} elapsed_seconds={elapsed:.3f} real_lease={hook == "after_interrupt_checkpoint"} status={view.status}'
    )
    assert elapsed <= 90
    assert view.status == ('waiting_input' if hook == 'after_interrupt_checkpoint' else 'partial')
    if hook == 'after_interrupt_checkpoint':
        assert elapsed >= 40
        assert ledger_count(ledger, 'question') == 1
        assert await platform.count_rows('interrupts') == 1
    else:
        assert await platform.count_rows('run_outcomes') == 1
        if hook == 'after_input_application':
            assert ledger_count(ledger, 'command') == 1
            assert (await row(platform, s.resume_requests, run_id=run.run_id))[
                'status'
            ] == 'applied'
    async with platform.backend.create_uow().begin_read() as uow:
        events = (
            (
                await uow.execute(
                    select(s.run_events.c.type).where(s.run_events.c.run_id == run.run_id)
                )
            )
            .scalars()
            .all()
        )
    assert events.count('run.completed') == (0 if hook == 'after_interrupt_checkpoint' else 1)


@pytest.mark.asyncio
async def test_applied_marker_at_exhaustion_is_consumed_without_fourth_execution(platform):
    from workbench_agent.jobs.reconciler import Reconciler

    p = InterruptPlatform(platform)
    run, w = await p.waiting_run()
    await p.resume(run, w, key='three')
    await p.apply_then_crash(run)
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(update(s.run_jobs).where(s.run_jobs.c.kind == 'resume').values(attempt=3))
    result = await Reconciler(platform.backend, p.worker.registry).recover(run.run_id)
    assert result.reason == 'RETRY_EXHAUSTED'
    assert await p.resume_status(run) == 'applied'
    assert (await row(platform, s.interrupts, interrupt_id=w['interrupt_id']))[
        'status'
    ] == 'consumed'


@pytest.mark.asyncio
async def test_native_next_checkpoint_authorizes_one_safe_recover_generation(platform):
    from support.rca_recovery import RcaRecovery

    p = RcaRecovery(platform)
    run, owned = await p.start(interrupt_after=['prepare'], conflict=False)
    await expire_lease(platform, owned.guard)
    assert (await p.registry.reconciler.recover(run.run_id)).action == 'continue'
    assert await p.worker.run_once() == 'projected'
    assert (await platform.get_run(run.run_id)).status == 'partial'


@pytest.mark.asyncio
async def test_corrupt_checkpoint_fails_closed_instead_of_perpetual_projection_retry(platform):
    from workbench_agent.jobs.reconciler import Reconciler
    from workbench_agent.runtime.graphs.registry import foundation_worker_registry

    run = await platform.create_run()
    owned = await claim(platform)
    registry = foundation_worker_registry(platform.backend)
    await registry._executors['conversation.multi_turn'].execute(Execution(platform.backend, owned))
    await expire_lease(platform, owned.guard)
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(update(s.graph_checkpoints).values(payload=b'corrupt'))
    result = await Reconciler(platform.backend, registry).recover(run.run_id)
    assert result.reason == 'STATE_RECONCILIATION_REQUIRED'
    assert (await platform.get_run(run.run_id)).status == 'failed'


@pytest.mark.asyncio
async def test_missing_pinned_version_keeps_projection_intent_and_epoch(platform):
    from workbench_agent.jobs.reconciler import Reconciler
    from workbench_agent.jobs.worker import ExecutorRegistry

    run = await platform.create_run()
    owned = await claim(platform)
    await expire_lease(platform, owned.guard)
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(update(s.runs).values(recovery_phase='finalizing'))
    result = await Reconciler(platform.backend, ExecutorRegistry()).recover(run.run_id)
    assert result.action == 'unavailable'
    saved = await row(platform, s.runs, run_id=run.run_id)
    assert saved['run_epoch'] == owned.guard.run_epoch and saved['recovery_phase'] == 'finalizing'


@pytest.mark.asyncio
async def test_final_checkpoint_recovery_supersedes_interrupted_draft_once(platform):
    from workbench_agent.jobs.reconciler import Reconciler
    from workbench_agent.runtime.graphs.registry import foundation_worker_registry
    from workbench_agent.persistence.events import RunEventStore
    from uuid import uuid4

    run = await platform.create_run()
    owned = await claim(platform)
    registry = foundation_worker_registry(platform.backend)
    event = await RunEventStore(platform.backend).append_execution(
        owned.guard, 'assistant.delta', {'text': 'interrupted draft'}
    )
    message = str(event.message_id)
    await registry._executors['conversation.multi_turn'].execute(Execution(platform.backend, owned))
    await expire_lease(platform, owned.guard)
    assert (await Reconciler(platform.backend, registry).recover(run.run_id)).action == 'finalized'
    async with platform.backend.create_uow().begin_read() as uow:
        rows = (
            (
                await uow.execute(
                    select(s.run_events.c.message_id).where(
                        s.run_events.c.type == 'assistant.message_superseded'
                    )
                )
            )
            .scalars()
            .all()
        )
    assert rows == [message]


@pytest.mark.asyncio
async def test_submitted_resume_before_command_safely_continues_without_new_question(platform):
    from support.rca_recovery import RcaRecovery

    p = RcaRecovery(platform)
    run, w = await p.waiting_run()
    await p.resume(run, w)
    owned = await claim(platform)
    await expire_lease(platform, owned.guard)
    assert (await p.registry.reconciler.recover(run.run_id)).action == 'continue'
    assert (await row(platform, s.resume_requests, run_id=run.run_id))['status'] == 'submitted'
    assert await p.worker.run_once() == 'projected'
    assert (await row(platform, s.resume_requests, run_id=run.run_id))['status'] == 'applied'
    assert await platform.count_rows('interrupts') == 1


@pytest.mark.asyncio
async def test_safe_continuation_generation_rolls_back_atomically(platform):
    from workbench_agent.jobs.reconciler import Reconciler
    from workbench_agent.runtime.graphs.registry import foundation_worker_registry

    run = await platform.create_run()
    owned = await claim(platform)
    await expire_lease(platform, owned.guard)
    registry = foundation_worker_registry(platform.backend)
    reconciler = Reconciler(platform.backend, registry)
    replacement = await claim(platform, 'projection')
    observation = await reconciler.prepare(replacement)
    with pytest.raises(RuntimeError, match='crash before commit'):
        async with platform.backend.create_uow().begin_write() as uow:
            assert (await reconciler.reconcile(uow, replacement, observation)).action == 'continue'
            raise RuntimeError('crash before commit')
    assert await platform.count_rows('run_jobs') == 1
    saved = await row(platform, s.runs, run_id=run.run_id)
    assert saved['recovery_phase'] == 'projection'
    await expire_lease(platform, replacement.guard)
    assert (await reconciler.recover(run.run_id)).action == 'continue'
    assert await platform.count_rows('run_jobs') == 2


@pytest.mark.asyncio
async def test_uncertain_external_action_is_never_replayed_by_foundation_recovery(platform):
    from workbench_agent.jobs.reconciler import Reconciler
    from workbench_agent.runtime.graphs.registry import foundation_worker_registry
    from sqlalchemy import insert
    from uuid import uuid4

    run = await platform.create_run()
    owned = await claim(platform)
    await expire_lease(platform, owned.guard)
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(
            insert(s.actions).values(
                team_id=platform.auth.team_id,
                run_id=run.run_id,
                action_id=str(uuid4()),
                action_key='unknown',
                tool_version='future@1',
                payload_hash='a' * 64,
                payload_ref='{}',
                scope='{}',
                state='unknown',
            )
        )
    result = await Reconciler(
        platform.backend, foundation_worker_registry(platform.backend)
    ).recover(run.run_id)
    assert result.reason == 'STATE_RECONCILIATION_REQUIRED'
    assert await platform.count_rows('run_jobs') == 1


@pytest.mark.asyncio
async def test_revoked_owner_is_parked_without_crashing_scheduler_or_replaying(platform):
    from workbench_agent.jobs.scheduler import Scheduler
    from workbench_agent.runtime.graphs.registry import foundation_worker_registry

    run = await platform.create_run()
    owned = await claim(platform)
    await expire_lease(platform, owned.guard)
    from workbench_agent.application.runs import RunService
    from workbench_agent.domain.runs import CreateRun
    from support.runs import make_run_payload

    async with platform.backend.create_uow().begin_read() as uow:
        other_agent = await platform.backend.configuration.one(
            uow, 'agents', team_id=platform.other_auth.team_id
        )
    other = await RunService(platform.backend).create(
        platform.other_auth,
        CreateRun.model_validate(make_run_payload(other_agent['agent_id'])),
        'other-recovery',
    )
    registry = foundation_worker_registry(platform.backend)
    async with platform.backend.create_uow().begin_write() as uow:
        candidates = await platform.backend.jobs.candidates(uow, run_id=other.run_id)
        other_owned, _ = await platform.backend.jobs.claim(
            uow, registry.capabilities(), 'other-worker', candidates
        )
    await registry._executors['conversation.multi_turn'].execute(
        Execution(platform.backend, other_owned)
    )
    await expire_lease(platform, other_owned.guard)
    team = await row(platform, s.teams, team_id=platform.auth.team_id)
    await platform.teams.set_member(
        platform.auth,
        platform.auth.subject,
        'admin',
        'revoked',
        expected_revision=team['revision'],
        key='revoke-recovery',
    )
    report = await Scheduler(platform.backend, foundation_worker_registry(platform.backend)).tick()
    assert report.unavailable == 1 and report.recovered == 1
    assert (
        await RunService(platform.backend).get(platform.other_auth, other.run_id)
    ).status == 'succeeded'
    job = await row(platform, s.run_jobs, job_id=owned.guard.job_id)
    saved = await row(platform, s.runs, run_id=run.run_id)
    assert job['status'] == 'capability_blocked' and job['attempt'] == 1
    assert saved['recovery_phase'] == 'projection'
    assert await platform.count_rows('execution_leases') == 0

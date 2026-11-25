"""Real SQLite budget ledger: reservations survive crashes and unknown receipts."""

import pytest
from workbench_agent.application.errors import DomainError
from workbench_agent.persistence.errors import StaleExecution
from support.jobs import claim, expire_lease, row
from workbench_agent.persistence import schema as s


@pytest.mark.asyncio
async def test_pending_receipt_is_not_free_and_reconciliation_is_idempotent(platform):
    from workbench_agent.runtime.budgets.service import BudgetService
    from workbench_agent.domain.usage import ReservationRequest, ActualUsage

    run = await platform.create_run()
    guard = (await claim(platform)).guard
    budgets = BudgetService(platform.backend)
    async with platform.backend.create_uow().begin_write() as uow:
        await budgets.reserve(
            guard,
            ReservationRequest(
                invocation_id='request-1',
                kind='evaluation',
                model_attempts=1,
                token_upper_bound=100,
            ),
            uow=uow,
        )
    async with platform.backend.create_uow().begin_write() as uow:
        await budgets.mark_sent(guard, 'request-1', uow=uow)
    async with platform.backend.create_uow().begin_write() as uow:
        result = await budgets.settle(guard, 'request-1', ActualUsage(), uow=uow)
    assert result.usage_pending and result.cost_pending
    pending = await budgets.snapshot(platform.auth, run.run_id)
    assert pending.model_attempts == 1 and pending.reserved_model_attempts == 0
    assert (
        pending.reserved_tokens == 100
        and pending.total_tokens is None
        and pending.amount_micros is None
    )
    usage = ActualUsage(
        input_tokens=11,
        output_tokens=7,
        amount_micros=42,
        currency='USD',
        pricing_version='price-1',
    )
    for _ in range(2):
        async with platform.backend.create_uow().begin_write() as uow:
            await budgets.settle(guard, 'request-1', usage, uow=uow)
    final = await budgets.snapshot(platform.auth, run.run_id)
    assert final.total_tokens == 18 and final.amount_micros == 42
    assert final.reserved_tokens == 0 and final.model_attempts == 1
    assert await platform.count_rows('usage_records') == 1
    async with platform.backend.create_uow().begin_write() as uow:
        with pytest.raises(DomainError, match='USAGE_CONFLICT'):
            await budgets.settle(
                guard, 'request-1', usage.model_copy(update={'input_tokens': 12}), uow=uow
            )


@pytest.mark.asyncio
async def test_reservation_rollback_unsent_release_and_sent_no_refund(platform):
    from workbench_agent.runtime.budgets.service import BudgetService
    from workbench_agent.domain.usage import ReservationRequest

    run = await platform.create_run()
    guard = (await claim(platform)).guard
    budgets = BudgetService(platform.backend)
    request = ReservationRequest(
        invocation_id='a', kind='expert', model_attempts=1, token_upper_bound=100
    )
    with pytest.raises(ValueError):
        async with platform.backend.create_uow().begin_write() as uow:
            await budgets.reserve(guard, request, uow=uow)
            raise ValueError('rollback caller transaction')
    assert (await budgets.snapshot(platform.auth, run.run_id)).reserved_model_attempts == 0
    async with platform.backend.create_uow().begin_write() as uow:
        await budgets.reserve(guard, request, uow=uow)
        await budgets.release_unsent(guard, 'a', uow=uow)
    async with platform.backend.create_uow().begin_write() as uow:
        await budgets.reserve(guard, request.model_copy(update={'invocation_id': 'b'}), uow=uow)
        await budgets.mark_sent(guard, 'b', uow=uow)
    async with platform.backend.create_uow().begin_write() as uow:
        with pytest.raises(DomainError, match='INVOCATION_POSSIBLY_SENT'):
            await budgets.release_unsent(guard, 'b', uow=uow)
    await expire_lease(platform, guard)
    assert (await budgets.snapshot(platform.auth, run.run_id)).model_attempts == 1
    assert (await budgets.snapshot(platform.auth, run.run_id)).reserved_tokens == 100


@pytest.mark.asyncio
@pytest.mark.parametrize('failure_after_mark_sent', [False, True])
async def test_atomic_reserve_and_send_mark_roll_back_before_dispatch(
    platform, failure_after_mark_sent
):
    from workbench_agent.runtime.budgets.service import BudgetService
    from workbench_agent.domain.usage import ReservationRequest

    run = await platform.create_run()
    guard = (await claim(platform)).guard
    budgets = BudgetService(platform.backend)
    request = ReservationRequest(
        invocation_id='atomic-attempt', kind='expert', model_attempts=1, token_upper_bound=100
    )
    with pytest.raises(RuntimeError, match='transaction failed'):
        async with platform.backend.create_uow().begin_write() as uow:
            await budgets.reserve(guard, request, uow=uow)
            if failure_after_mark_sent:
                await budgets.mark_sent(guard, request.invocation_id, uow=uow)
            raise RuntimeError('transaction failed')
    snapshot = await budgets.snapshot(platform.auth, run.run_id)
    assert snapshot.reserved_model_attempts == 0
    assert snapshot.model_attempts == 0
    assert await platform.count_rows('usage_reservations') == 0


@pytest.mark.asyncio
async def test_stale_settlement_is_quarantined_then_current_owner_reconciles(platform):
    from workbench_agent.runtime.budgets.service import BudgetService
    from workbench_agent.domain.usage import ReservationRequest, ActualUsage

    run = await platform.create_run()
    old = (await claim(platform)).guard
    budgets = BudgetService(platform.backend)
    async with platform.backend.create_uow().begin_write() as uow:
        await budgets.reserve(
            old,
            ReservationRequest(
                invocation_id='a', kind='model', model_attempts=1, token_upper_bound=100
            ),
            uow=uow,
        )
        await budgets.mark_sent(old, 'a', uow=uow)
    await expire_lease(platform, old)
    usage = ActualUsage(input_tokens=30, output_tokens=20)
    with pytest.raises(StaleExecution):
        async with platform.backend.create_uow().begin_write() as uow:
            await budgets.settle(old, 'a', usage, uow=uow)
    async with platform.backend.create_uow().begin_write() as uow:
        await budgets.quarantine(old, 'a', usage, uow=uow)
    assert await platform.count_rows('usage_records') == 0
    assert await platform.count_rows('usage_quarantine') == 1
    current = (await claim(platform, 'replacement')).guard
    async with platform.backend.create_uow().begin_write() as uow:
        await budgets.reconcile(current, 'a', uow=uow)
    summary = await budgets.snapshot(platform.auth, run.run_id)
    assert summary.total_tokens == 50 and summary.cost_pending and summary.amount_micros is None
    assert summary.model_attempts == 1


@pytest.mark.asyncio
async def test_snapshot_cross_team_and_revoked_writer_are_hidden(platform):
    from workbench_agent.runtime.budgets.service import BudgetService
    from workbench_agent.domain.usage import ReservationRequest
    from sqlalchemy import update

    run = await platform.create_run()
    guard = (await claim(platform)).guard
    budgets = BudgetService(platform.backend)
    with pytest.raises(DomainError, match='NOT_FOUND'):
        await budgets.snapshot(platform.other_auth, run.run_id)
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(
            update(s.team_members)
            .where(s.team_members.c.subject == platform.auth.subject)
            .values(status='revoked')
        )
    with pytest.raises(DomainError, match='NOT_FOUND'):
        async with platform.backend.create_uow().begin_write() as uow:
            await budgets.reserve(
                guard, ReservationRequest(invocation_id='a', kind='tool', tool_attempts=1), uow=uow
            )


@pytest.mark.asyncio
async def test_cost_and_tokens_reconcile_independently_and_overage_is_recorded(platform):
    from workbench_agent.runtime.budgets.service import BudgetService
    from workbench_agent.domain.usage import ReservationRequest, ActualUsage

    run = await platform.create_run(token_limit=2000, summary_token_reserve=1000)
    guard = (await claim(platform)).guard
    budgets = BudgetService(platform.backend, summary_tokens=1000)
    async with platform.backend.create_uow().begin_write() as uow:
        await budgets.reserve(
            guard,
            ReservationRequest(
                invocation_id='a', kind='evaluation', model_attempts=1, token_upper_bound=1000
            ),
            uow=uow,
        )
        await budgets.mark_sent(guard, 'a', uow=uow)
    async with platform.backend.create_uow().begin_write() as uow:
        receipt = await budgets.settle(
            guard, 'a', ActualUsage(amount_micros=9, currency='USD', pricing_version='p1'), uow=uow
        )
    assert receipt.usage_pending and not receipt.cost_pending
    snapshot = await budgets.snapshot(platform.auth, run.run_id)
    assert (
        snapshot.amount_micros == 9
        and snapshot.total_tokens is None
        and snapshot.reserved_tokens == 1000
    )
    async with platform.backend.create_uow().begin_write() as uow:
        await budgets.settle(
            guard, 'a', ActualUsage(input_tokens=1500, output_tokens=1000), uow=uow
        )
    snapshot = await budgets.snapshot(platform.auth, run.run_id)
    assert (
        snapshot.total_tokens == 2500
        and snapshot.token_overage == 500
        and snapshot.amount_micros == 9
    )
    with pytest.raises(DomainError, match='BUDGET_EXCEEDED'):
        async with platform.backend.create_uow().begin_write() as uow:
            await budgets.reserve(
                guard,
                ReservationRequest(
                    invocation_id='b', kind='summary', model_attempts=1, token_upper_bound=1
                ),
                uow=uow,
            )


@pytest.mark.asyncio
async def test_frozen_model_context_limit_is_independent_of_parent_budget(platform):
    from workbench_agent.runtime.budgets.service import BudgetService
    from workbench_agent.domain.usage import ReservationRequest

    await platform.create_run()
    guard = (await claim(platform)).guard
    with pytest.raises(DomainError, match='MAX_CONTEXT_EXCEEDED'):
        async with platform.backend.create_uow().begin_write() as uow:
            await BudgetService(platform.backend, summary_tokens=1).reserve(
                guard,
                ReservationRequest(
                    invocation_id='a', kind='model', model_attempts=1, token_upper_bound=64001
                ),
                uow=uow,
            )


@pytest.mark.asyncio
async def test_reservation_payload_conflict_and_unsent_receipt_rejected(platform):
    from workbench_agent.runtime.budgets.service import BudgetService
    from workbench_agent.domain.usage import ReservationRequest, ActualUsage

    await platform.create_run()
    guard = (await claim(platform)).guard
    budgets = BudgetService(platform.backend)
    request = ReservationRequest(invocation_id='a', kind='tool', tool_attempts=1)
    async with platform.backend.create_uow().begin_write() as uow:
        first = await budgets.reserve(guard, request, uow=uow)
        second = await budgets.reserve(guard, request, uow=uow)
        assert first.reservation_id == second.reservation_id
        with pytest.raises(DomainError, match='RESERVATION_CONFLICT'):
            await budgets.reserve(
                guard, request.model_copy(update={'token_upper_bound': 1}), uow=uow
            )
        with pytest.raises(DomainError, match='INVOCATION_NOT_SENT'):
            await budgets.settle(guard, 'a', ActualUsage(input_tokens=0, output_tokens=0), uow=uow)


@pytest.mark.asyncio
async def test_dispatch_confirmation_cannot_create_or_repeat_permission(platform):
    from workbench_agent.domain.usage import ActualUsage

    await platform.create_run()
    guard = (await claim(platform)).guard
    await platform.reserve_usage(guard, invocation_id='a')
    async with platform.backend.create_uow().begin_write() as uow:
        with pytest.raises(DomainError, match='INVOCATION_NOT_SENT'):
            await platform.budgets.mark_sent(guard, 'a', confirmed=True, uow=uow)
    async with platform.backend.create_uow().begin_write() as uow:
        permit = await platform.budgets.mark_sent(guard, 'a', uow=uow)
    assert permit.send_state == 'possibly_sent' and permit.dispatch_allowed
    async with platform.backend.create_uow().begin_write() as uow:
        confirmed = await platform.budgets.mark_sent(guard, 'a', confirmed=True, uow=uow)
    assert confirmed.send_state == 'sent' and not confirmed.dispatch_allowed


@pytest.mark.asyncio
async def test_quarantine_rejects_forged_owner_and_audits_isolated_receipt(platform):
    from dataclasses import replace
    from workbench_agent.domain.usage import ActualUsage

    await platform.create_run()
    guard = (await claim(platform)).guard
    await platform.reserve_usage(guard, invocation_id='a')
    async with platform.backend.create_uow().begin_write() as uow:
        await platform.budgets.mark_sent(guard, 'a', uow=uow)
    async with platform.backend.create_uow().begin_write() as uow:
        with pytest.raises(DomainError, match='NOT_FOUND'):
            await platform.budgets.quarantine(
                replace(guard, worker_id='forged'), 'a', ActualUsage(), uow=uow
            )
    async with platform.backend.create_uow().begin_write() as uow:
        await platform.budgets.quarantine(guard, 'a', ActualUsage(), uow=uow)
        await platform.budgets.quarantine(guard, 'a', ActualUsage(), uow=uow)
    from sqlalchemy import select

    async with platform.backend.create_uow().begin_read() as uow:
        audits = (
            (
                await uow.execute(
                    select(s.audit_logs).where(s.audit_logs.c.operation == 'usage.quarantine')
                )
            )
            .mappings()
            .all()
        )
    assert len(audits) == 1 and audits[0]['decision'] == 'isolated'


@pytest.mark.asyncio
async def test_snapshot_uses_authoritative_active_execution_intervals(platform):
    from sqlalchemy import update

    await platform.create_run()
    guard = (await claim(platform)).guard
    async with platform.backend.create_uow().begin_write() as uow:
        now = await platform.backend.clock.now_ms(uow)
        await uow.execute(
            update(s.runs)
            .where(s.runs.c.run_id == guard.run_id)
            .values(active_elapsed_ms=1200, active_since=now - 500)
        )
    snapshot = await platform.budgets.snapshot(platform.auth, guard.run_id)
    assert 1700 <= snapshot.active_elapsed_ms < 2500
    async with platform.backend.create_uow().begin_write() as uow:
        await platform.backend.jobs.release(
            uow, guard, status='queued', job_status='retry_wait', retry_delay_ms=10000
        )
    first = await platform.budgets.snapshot(platform.auth, guard.run_id)
    second = await platform.budgets.snapshot(platform.auth, guard.run_id)
    assert first.active_elapsed_ms == second.active_elapsed_ms


@pytest.mark.asyncio
async def test_budget_active_deadline_blocks_new_attempts(platform):
    from sqlalchemy import update

    await platform.create_run(wall_seconds=1)
    guard = (await claim(platform)).guard
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(
            update(s.runs).where(s.runs.c.run_id == guard.run_id).values(active_elapsed_ms=1000)
        )
    with pytest.raises(DomainError, match='BUDGET_EXCEEDED'):
        await platform.reserve_usage(guard, invocation_id='late')


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['reserve', 'dispatch'])
async def test_finalizing_cannot_issue_a_new_network_permit(platform, operation):
    from sqlalchemy import update

    await platform.create_run()
    guard = (await claim(platform)).guard
    if operation == 'dispatch':
        await platform.reserve_usage(guard, invocation_id='a')
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(
            update(s.runs).where(s.runs.c.run_id == guard.run_id).values(status='finalizing')
        )
    with pytest.raises(StaleExecution):
        if operation == 'reserve':
            await platform.reserve_usage(guard, invocation_id='a')
        else:
            async with platform.backend.create_uow().begin_write() as uow:
                await platform.budgets.mark_sent(guard, 'a', uow=uow)
    snapshot = await platform.budgets.snapshot(platform.auth, guard.run_id)
    assert snapshot.model_attempts == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('deadline', ['active', 'wall'])
async def test_dispatch_rechecks_deadline_after_reservation(platform, deadline):
    from sqlalchemy import update

    await platform.create_run(wall_seconds=1)
    guard = (await claim(platform)).guard
    await platform.reserve_usage(guard, invocation_id='a')
    async with platform.backend.create_uow().begin_write() as uow:
        changes = {'active_elapsed_ms': 1000} if deadline == 'active' else {'wall_deadline_at': 0}
        await uow.execute(update(s.runs).where(s.runs.c.run_id == guard.run_id).values(**changes))
    with pytest.raises(DomainError, match='BUDGET_EXCEEDED'):
        async with platform.backend.create_uow().begin_write() as uow:
            await platform.budgets.mark_sent(guard, 'a', uow=uow)
    snapshot = await platform.budgets.snapshot(platform.auth, guard.run_id)
    assert snapshot.model_attempts == 0 and snapshot.reserved_model_attempts == 1


@pytest.mark.asyncio
async def test_finalizing_can_account_an_already_sent_attempt(platform):
    from sqlalchemy import update
    from workbench_agent.domain.usage import ActualUsage

    await platform.create_run()
    guard = (await claim(platform)).guard
    await platform.reserve_usage(guard, invocation_id='a')
    async with platform.backend.create_uow().begin_write() as uow:
        await platform.budgets.mark_sent(guard, 'a', uow=uow)
        await uow.execute(
            update(s.runs).where(s.runs.c.run_id == guard.run_id).values(status='finalizing')
        )
    async with platform.backend.create_uow().begin_write() as uow:
        await platform.budgets.settle(
            guard, 'a', ActualUsage(input_tokens=1, output_tokens=2), uow=uow
        )
    assert (await platform.budgets.snapshot(platform.auth, guard.run_id)).total_tokens == 3


@pytest.mark.asyncio
async def test_partial_outcome_keeps_unknown_aggregate_amount_and_currency_empty(platform):
    from workbench_agent.domain.usage import ActualUsage

    run = await platform.create_run()
    guard = (await claim(platform)).guard
    for invocation in ('priced', 'unpriced'):
        await platform.reserve_usage(guard, invocation_id=invocation)
        async with platform.backend.create_uow().begin_write() as uow:
            await platform.budgets.mark_sent(guard, invocation, uow=uow)
            usage = (
                ActualUsage(
                    input_tokens=1,
                    output_tokens=1,
                    amount_micros=9,
                    currency='USD',
                    pricing_version='p1',
                )
                if invocation == 'priced'
                else ActualUsage(input_tokens=1, output_tokens=1)
            )
            await platform.budgets.settle(guard, invocation, usage, uow=uow)
    result = await platform.budgets.partial_outcome(platform.auth, run.run_id)
    assert result.usage_summary.status == 'pending'
    assert result.usage_summary.amount_micros is None and result.usage_summary.currency is None
    assert result.usage_summary.input_tokens == 2 and result.usage_summary.output_tokens == 2
    assert (await platform.budgets.snapshot(platform.auth, run.run_id)).known_costs == {'USD': 9}


@pytest.mark.asyncio
async def test_partial_outcome_truncates_maximum_summary_without_losing_evidence(platform):
    from workbench_agent.domain.outcomes import RunOutcome, Finding, UsageSummary
    from uuid import UUID

    run = await platform.create_run()
    evidence = UUID('00000000-0000-0000-0000-000000000001')
    prior = RunOutcome(
        kind='analysis.v1',
        completed=True,
        summary='界' * 65536,
        findings=(Finding(summary='Observed failure', evidence_refs=(evidence,)),),
        root_cause_hypotheses=(),
        impact=None,
        evidence_refs=(evidence,),
        unresolved_items=(),
        proposed_actions=(),
        knowledge_gaps=(),
        usage_summary=UsageSummary(
            status='complete', input_tokens=0, output_tokens=0, amount_micros=0, currency='USD'
        ),
    )
    result = await platform.budgets.partial_outcome(platform.auth, run.run_id, prior=prior)
    assert len(result.summary) == 65536 and result.summary.startswith(
        'Budget-limited partial result. '
    )
    assert result.evidence_refs == (evidence,) and result.findings[0].evidence_refs == (evidence,)
    assert result.findings[0].summary == 'Observed failure' and not result.completed
    assert prior.summary == '界' * 65536

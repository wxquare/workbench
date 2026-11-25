"""Experts, evaluations and real retry attempts share the parent Run ledger."""

import asyncio
import pytest
from workbench_agent.application.errors import DomainError
from workbench_agent.domain.usage import ReservationRequest, ActualUsage
from workbench_agent.runtime.budgets.service import BudgetService
from support.jobs import claim, expire_lease


@pytest.mark.asyncio
async def test_concurrent_experts_cannot_spend_final_summary_call(platform):
    run = await platform.create_run(model_call_limit=2)
    guard = (await claim(platform)).guard
    results = await asyncio.gather(
        platform.reserve_usage(guard, invocation_id='a'),
        platform.reserve_usage(guard, invocation_id='b'),
        return_exceptions=True,
    )
    assert sum(platform.is_reserved(r) for r in results) == 1
    assert sum(isinstance(r, DomainError) and r.code == 'BUDGET_EXCEEDED' for r in results) == 1
    assert (await platform.budgets.snapshot(platform.auth, run.run_id)).reserved_model_attempts == 1
    summary = await platform.reserve_usage(
        guard, invocation_id='summary', kind='summary', token_upper_bound=64000
    )
    assert platform.is_reserved(summary)


@pytest.mark.asyncio
async def test_limit_one_expert_is_rejected_but_summary_can_run(platform):
    run = await platform.create_run(model_call_limit=1)
    guard = (await claim(platform)).guard
    with pytest.raises(DomainError, match='BUDGET_EXCEEDED'):
        await platform.reserve_usage(guard, invocation_id='expert')
    await platform.reserve_usage(
        guard, invocation_id='summary', kind='summary', token_upper_bound=64000
    )
    result = await platform.budgets.snapshot(platform.auth, run.run_id)
    assert result.reserved_model_attempts == 1 and result.reserved_tokens == 64000


@pytest.mark.asyncio
async def test_default_summary_hold_leaves_sixteen_thousand_tokens(platform):
    await platform.create_run()
    guard = (await claim(platform)).guard
    await platform.reserve_usage(guard, invocation_id='fits', token_upper_bound=16000)
    with pytest.raises(DomainError, match='BUDGET_EXCEEDED'):
        await platform.reserve_usage(guard, invocation_id='too-many', token_upper_bound=1)


@pytest.mark.asyncio
async def test_downward_budget_below_summary_hold_requires_deterministic_partial(platform):
    run = await platform.create_run(token_limit=63999)
    guard = (await claim(platform)).guard
    with pytest.raises(DomainError, match='BUDGET_EXCEEDED'):
        await platform.reserve_usage(guard, invocation_id='expert', token_upper_bound=1)
    with pytest.raises(DomainError, match='BUDGET_EXCEEDED'):
        await platform.reserve_usage(
            guard, invocation_id='summary', kind='summary', token_upper_bound=64000
        )
    with pytest.raises(DomainError, match='BUDGET_EXCEEDED'):
        await platform.reserve_usage(
            guard, invocation_id='summary-small-request', kind='summary', token_upper_bound=1
        )
    outcome = await platform.budgets.partial_outcome(platform.auth, run.run_id)
    assert not outcome.completed and outcome.unresolved_items
    assert outcome.kind == 'analysis.v1' and outcome.usage_summary.model_calls == 0
    assert 'budget' in outcome.summary.lower()


@pytest.mark.asyncio
async def test_explicit_summary_bound_cannot_be_borrowed_or_disabled(platform):
    await platform.create_run(token_limit=2000, summary_token_reserve=1000)
    guard = (await claim(platform)).guard
    budgets = BudgetService(platform.backend, summary_tokens=1000)
    async with platform.backend.create_uow().begin_write() as uow:
        await budgets.reserve(
            guard,
            ReservationRequest(
                invocation_id='expert', kind='expert', model_attempts=1, token_upper_bound=1000
            ),
            uow=uow,
        )
    async with platform.backend.create_uow().begin_write() as uow:
        with pytest.raises(DomainError, match='SUMMARY_RESERVE_REQUIRED'):
            await budgets.reserve(
                guard,
                ReservationRequest(
                    invocation_id='summary-too-large',
                    kind='summary',
                    model_attempts=1,
                    token_upper_bound=1001,
                ),
                uow=uow,
            )
        await budgets.reserve(
            guard,
            ReservationRequest(
                invocation_id='summary', kind='summary', model_attempts=1, token_upper_bound=1000
            ),
            uow=uow,
        )
    with pytest.raises(ValueError):
        BudgetService(platform.backend, summary_tokens=0)


@pytest.mark.asyncio
async def test_three_real_attempts_not_job_attempts_and_recovery_does_not_refresh(platform):
    run = await platform.create_run(model_call_limit=4)
    guard = (await claim(platform)).guard
    for index in range(3):
        invocation = f'retry-{index}'
        await platform.reserve_usage(guard, invocation_id=invocation)
        async with platform.backend.create_uow().begin_write() as uow:
            permit = await platform.budgets.mark_sent(guard, invocation, uow=uow)
        assert permit.dispatch_allowed
        async with platform.backend.create_uow().begin_write() as uow:
            replay = await platform.budgets.mark_sent(guard, invocation, uow=uow)
            await platform.budgets.settle(
                guard, invocation, ActualUsage(input_tokens=5, output_tokens=5), uow=uow
            )
        assert not replay.dispatch_allowed
    await expire_lease(platform, guard)
    current = (await claim(platform, 'next')).guard
    with pytest.raises(DomainError, match='BUDGET_EXCEEDED'):
        await platform.reserve_usage(current, invocation_id='fourth')
    result = await platform.budgets.snapshot(platform.auth, run.run_id)
    assert result.model_attempts == 3 and result.total_tokens == 30
    assert result.cost_pending and result.amount_micros is None


@pytest.mark.asyncio
async def test_takeover_cannot_shrink_the_persisted_summary_hold(platform):
    run = await platform.create_run(token_limit=3000, summary_token_reserve=2000)
    old = (await claim(platform)).guard
    original = BudgetService(platform.backend, summary_tokens=2000)
    async with platform.backend.create_uow().begin_write() as uow:
        await original.reserve(
            old,
            ReservationRequest(
                invocation_id='expert-a', kind='expert', model_attempts=1, token_upper_bound=1000
            ),
            uow=uow,
        )
    await expire_lease(platform, old)
    current = (await claim(platform, 'new')).guard
    smaller = BudgetService(platform.backend, summary_tokens=1000)
    with pytest.raises(DomainError, match='BUDGET_POLICY_CONFLICT'):
        async with platform.backend.create_uow().begin_write() as uow:
            await smaller.reserve(
                current,
                ReservationRequest(
                    invocation_id='expert-b', kind='expert', model_attempts=1, token_upper_bound=1
                ),
                uow=uow,
            )
    # An unspecified config adopts the fixed Run policy rather than recomputing
    # either a smaller or larger process-local default on recovery.
    with pytest.raises(DomainError, match='BUDGET_EXCEEDED'):
        await platform.reserve_usage(current, invocation_id='expert-c', token_upper_bound=1)
    snapshot = await platform.budgets.snapshot(platform.auth, run.run_id)
    assert snapshot.summary_token_reserve == 2000 and snapshot.reserved_tokens == 1000


@pytest.mark.asyncio
@pytest.mark.parametrize('reject_first', [False, True])
async def test_admission_policy_survives_takeover_before_first_successful_reservation(
    platform, reject_first
):
    run = await platform.create_run(token_limit=64000)
    old = (await claim(platform)).guard
    if reject_first:
        with pytest.raises(DomainError, match='BUDGET_EXCEEDED'):
            await platform.reserve_usage(old, invocation_id='rejected')
    await expire_lease(platform, old)
    current = (await claim(platform, 'recovered')).guard
    smaller = BudgetService(platform.backend, summary_tokens=1000)
    with pytest.raises(DomainError, match='BUDGET_POLICY_CONFLICT'):
        async with platform.backend.create_uow().begin_write() as uow:
            await smaller.reserve(
                current,
                ReservationRequest(
                    invocation_id='attempt', kind='expert', model_attempts=1, token_upper_bound=100
                ),
                uow=uow,
            )
    snapshot = await platform.budgets.snapshot(platform.auth, run.run_id)
    assert snapshot.summary_token_reserve == 64000 and snapshot.reserved_model_attempts == 0


@pytest.mark.asyncio
async def test_public_admission_can_pin_explicit_summary_token_reserve(client, platform):
    from support.runs import make_run_payload

    payload = make_run_payload(platform.agent_id)
    payload.update(summary_token_reserve=1000, budgets={'tokens': 2000})
    response = await client.post(
        '/v1/runs', json=payload, headers={'Idempotency-Key': 'explicit-summary-policy'}
    )
    assert response.status_code == 202
    summary = await platform.budgets.snapshot(platform.auth, response.json()['run_id'])
    assert summary.summary_token_reserve == 1000 and summary.reserved_model_attempts == 0


@pytest.mark.asyncio
async def test_admission_uses_frozen_summary_role_model_before_first_claim(platform):
    from uuid import uuid4
    from workbench_agent.settings import ModelProfile
    from workbench_agent.domain.profiles import ProfileDraft, RoleModelBinding

    model = await platform.publish_model_version(ModelProfile(max_context_tokens=32768))
    async with platform.backend.create_uow().begin_read() as uow:
        published = await platform.profiles.published(uow, platform.auth, platform.agent_id)
    current = await platform.profiles.get_agent(platform.auth, platform.agent_id)
    changed = await platform.profiles.save_draft(
        platform.auth,
        platform.agent_id,
        ProfileDraft(
            default_model=published.default_model,
            graph_id='conversation.multi_turn',
            role_models=(RoleModelBinding(role='summary', ref=model.ref),),
        ),
        expected_revision=current['revision'],
        key=str(uuid4()),
    )
    await platform.profiles.publish(
        platform.auth, platform.agent_id, expected_revision=changed['revision'], key=str(uuid4())
    )
    run = await platform.create_run()
    assert (
        await platform.budgets.snapshot(platform.auth, run.run_id)
    ).summary_token_reserve == 32768
    view = await platform.get_run(run.run_id)
    assert view.context.default_model == published.default_model
    assert view.context.model_for('summary') == model.ref


@pytest.mark.asyncio
async def test_bounded_summary_graph_uses_compact_input_policy(platform):
    from uuid import uuid4
    from workbench_agent.domain.profiles import ProfileDraft

    context = await platform.contexts.resolve(platform.auth, platform.agent_id)
    from workbench_agent.domain.tools import ToolBinding
    from workbench_agent.tools.registry import ToolRegistry

    binding = ToolBinding(
        alias='kb.search',
        provider='mcp',
        tool_name='knowledge_search',
        server='local',
        credential_scope='read',
        permission='knowledge.read',
        input_schema={'type': 'object', 'properties': {'query': {'type': 'string'}}},
    )
    ref = await ToolRegistry(platform.backend).publish(
        platform.auth, platform.agent_id, binding, key=str(uuid4())
    )
    current = await platform.profiles.get_agent(platform.auth, platform.agent_id)
    changed = await platform.profiles.save_draft(
        platform.auth,
        platform.agent_id,
        ProfileDraft(
            default_model=context.default_model,
            graph_id='conversation.tool_use',
            tools=(ref,),
        ),
        expected_revision=current['revision'],
        key=str(uuid4()),
    )
    await platform.profiles.publish(
        platform.auth,
        platform.agent_id,
        expected_revision=changed['revision'],
        key=str(uuid4()),
    )
    from support.runs import make_run_payload
    from workbench_agent.application.runs import RunService
    from workbench_agent.domain.runs import CreateRun

    payload = make_run_payload(platform.agent_id)
    from test_tool_conversation_graph import SCOPE

    payload['business_context'] = {
        'conversation': True,
        'query_scope': dict(
            SCOPE, start_at='2026-11-13T00:00:00.000Z', end_at='2026-11-13T00:10:00.000Z'
        ),
    }
    run = await RunService(platform.backend).create(
        platform.auth, CreateRun.model_validate(payload), 'bounded-summary'
    )
    summary = await platform.budgets.snapshot(platform.auth, run.run_id)

    assert summary.summary_token_reserve == 16_384 + 256 * 8 + 4_096


@pytest.mark.asyncio
async def test_admission_rolls_back_summary_policy_with_run_failure(platform, monkeypatch):
    async def fail_after_admission_writes(*args, **kwargs):
        raise RuntimeError('test admission event storage failure')

    receipts_before = await platform.count_rows('idempotency_records')
    monkeypatch.setattr(
        platform.backend.execution_events, 'append_control', fail_after_admission_writes
    )
    with pytest.raises(RuntimeError, match='test admission event storage failure'):
        await platform.create_run(summary_token_reserve=1000)
    assert await platform.count_rows('runs') == 0
    assert await platform.count_rows('usage_budget_policies') == 0
    assert await platform.count_rows('idempotency_records') == receipts_before


@pytest.mark.asyncio
async def test_public_admission_rejects_summary_reserve_above_frozen_context(client, platform):
    from support.runs import make_run_payload

    payload = make_run_payload(platform.agent_id)
    payload['summary_token_reserve'] = 64001
    response = await client.post(
        '/v1/runs', json=payload, headers={'Idempotency-Key': 'invalid-summary-policy'}
    )
    assert response.status_code == 422 and response.json()['code'] == 'SUMMARY_RESERVE_INVALID'
    assert await platform.count_rows('usage_budget_policies') == 0
    assert await platform.count_rows('runs') == 0

import pytest
from sqlalchemy import update
from workbench_agent.application.errors import DomainError
from workbench_agent.persistence import schema as s
from support.events import EventPlatform
from support.jobs import row, expire_lease, claim


@pytest.mark.asyncio
async def test_finalization_is_idempotent_and_events_replay(platform):
    p = EventPlatform(platform)
    run, guard, outcome = await p.finalizable_run()
    first = await p.results.finalize(guard, outcome, outcome.digest())
    second = await p.results.finalize(guard, outcome, outcome.digest())
    assert first == second and first.status == 'succeeded'
    assert first.outcome['summary'] == outcome.summary
    events = await p.read_events(run.run_id)
    terminal = [e for e in events if e.type == 'run.completed']
    assert len(terminal) == 1 and terminal[0].payload['terminal_status'] == 'succeeded'
    assert terminal[0].message_id and terminal[0].attempt == 1
    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    assert await platform.count_rows('run_outcomes') == 1
    assert await platform.count_rows('execution_leases') == 0
    assert (await row(platform, s.threads, thread_id=run.thread_id))['active_run_id'] is None
    assert (await row(platform, s.teams, team_id=platform.auth.team_id))['active_executions'] == 0
    assert await platform.count_rows('outbox') == 0
    with pytest.raises(DomainError) as error:
        changed = outcome.model_copy(update={'summary': 'Changed'})
        await p.results.finalize(guard, changed, changed.digest())
    assert error.value.status == 409


@pytest.mark.asyncio
async def test_stale_or_revoked_finalization_cannot_write_or_replay(platform):
    from workbench_agent.persistence.errors import StaleExecution

    p = EventPlatform(platform)
    run, guard, outcome = await p.finalizable_run()
    await expire_lease(platform, guard)
    new = await claim(platform, 'new')
    with pytest.raises(StaleExecution):
        await p.results.finalize(guard, outcome, outcome.digest())
    await p.results.finalize(new.guard, outcome, outcome.digest())
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(
            update(s.team_members)
            .where(s.team_members.c.team_id == platform.auth.team_id)
            .values(status='revoked')
        )
    with pytest.raises(DomainError):
        await p.results.finalize(new.guard, outcome, outcome.digest())


@pytest.mark.asyncio
async def test_direct_worker_projects_honest_foundation_outcome(platform):
    from support.interrupts import InterruptPlatform

    p = InterruptPlatform(platform)
    await p.publish_v2()
    run = await platform.create_run()
    assert await p.worker.run_once() == 'projected'
    view = await platform.get_run(run.run_id)
    assert view.status == 'succeeded' and view.outcome['kind'] == 'foundation.recorded.v1'
    assert not view.outcome['findings'] and not view.outcome['proposed_actions']
    assert 'diagnos' in view.outcome['summary'].lower()


@pytest.mark.asyncio
async def test_result_and_terminal_event_rollback_together(platform, monkeypatch):
    p = EventPlatform(platform)
    run, guard, outcome = await p.finalizable_run()
    original = platform.backend.jobs.release

    async def fail(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError('crash before commit')

    monkeypatch.setattr(platform.backend.jobs, 'release', fail)
    with pytest.raises(RuntimeError):
        await p.results.finalize(guard, outcome, outcome.digest())
    assert await platform.count_rows('run_outcomes') == 0
    assert (await platform.get_run(run.run_id)).status == 'running'
    assert await platform.count_rows('execution_leases') == 1
    assert not any(e.type == 'run.completed' for e in await p.read_events(run.run_id))
    monkeypatch.setattr(platform.backend.jobs, 'release', original)
    assert (await p.results.finalize(guard, outcome, outcome.digest())).status == 'succeeded'


@pytest.mark.asyncio
async def test_evidence_scope_quality_and_real_ids_are_validated(platform):
    from support.events import evidence
    from workbench_agent.domain.outcomes import RunOutcome
    from uuid import uuid4

    p = EventPlatform(platform)
    run, guard, base = await p.finalizable_run()
    old = await evidence(platform, run, region='ID')
    current = await evidence(platform, run, quality='unavailable', region='PH')
    data = base.model_dump(mode='json')
    data['kind'] = 'analysis.v1'
    for ref, status, code in [
        (str(uuid4()), 'observed', 'INVALID_EVIDENCE_SCOPE'),
        (old, 'normal', 'INVALID_EVIDENCE_SCOPE'),
        (current, 'normal', 'INVALID_NORMAL_EVIDENCE'),
    ]:
        data.update(
            evidence_refs=[ref],
            findings=[{'summary': 'Observation', 'evidence_refs': [ref], 'status': status}],
        )
        outcome = RunOutcome.model_validate(data)
        with pytest.raises(DomainError) as error:
            await p.results.finalize(guard, outcome, outcome.digest())
        assert error.value.code == code
    data.update(
        completed=False,
        unresolved_items=['Data unavailable'],
        findings=[
            {'summary': 'Source unavailable', 'evidence_refs': [current], 'status': 'uncertain'}
        ],
    )
    outcome = RunOutcome.model_validate(data)
    assert (await p.results.finalize(guard, outcome, outcome.digest())).status == 'partial'


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'saved,wall,want',
    [('valid', False, 'partial'), ('invalid', False, 'timed_out'), ('valid', True, 'expired')],
)
async def test_budget_uses_only_valid_saved_outcome_and_wall_always_wins(
    platform, saved, wall, want
):
    from support.events import save_outcome
    from workbench_agent.jobs.deadlines import DeadlineService

    p = EventPlatform(platform)
    run, guard, outcome = await p.finalizable_run(complete=False)
    await save_outcome(
        platform,
        guard,
        outcome.model_dump(mode='json') if saved == 'valid' else {'summary': 'not an Outcome'},
    )
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(
            update(s.runs)
            .where(s.runs.c.run_id == run.run_id)
            .values(deadline_at=0, **({'wall_deadline_at': 0} if wall else {}))
        )
    assert await DeadlineService(platform.backend).expire_owned(guard)
    view = await platform.get_run(run.run_id)
    assert view.status == want
    assert (view.outcome is not None) == (want == 'partial')
    events = await p.read_events(run.run_id)
    terminal = [e for e in events if e.type in {'run.completed', 'run.failed', 'run.expired'}]
    assert len(terminal) == 1 and terminal[0].payload['terminal_status'] == want


@pytest.mark.asyncio
async def test_final_answer_is_separate_message_and_public_messages_remain_readable(platform):
    from workbench_agent.application.runs import RunService

    p = EventPlatform(platform)
    run, guard, outcome = await p.finalizable_run()
    draft = await p.events.append_execution(guard, 'assistant.delta', {'text': 'Draft'})
    await p.results.finalize(guard, outcome, outcome.digest())
    final = next(e for e in await p.read_events(run.run_id) if e.type == 'run.completed')
    assert final.message_id != draft.message_id
    messages = await RunService(platform.backend).messages(platform.auth, run.thread_id)
    assert len(messages['items']) == 2
    assert (
        next(m for m in messages['items'] if m['role'] == 'assistant')['outcome']['summary']
        == outcome.summary
    )


@pytest.mark.asyncio
async def test_retry_supersedes_draft_and_attempt_is_not_lease_epoch(platform):
    from workbench_agent.jobs.leases import LeaseService

    p = EventPlatform(platform)
    run, guard, outcome = await p.finalizable_run()
    first = await p.events.append_execution(guard, 'assistant.delta', {'text': 'Old draft'})
    await LeaseService(platform.backend).release(guard, status='queued', job_status='retry_wait')
    new = await claim(platform, 'second')
    second = await p.events.append_execution(new.guard, 'assistant.delta', {'text': 'New draft'})
    events = await p.read_events(run.run_id)
    superseded = [e for e in events if e.type == 'assistant.message_superseded']
    assert len(superseded) == 1 and superseded[0].payload['message_id'] == first.message_id
    assert second.attempt == 2 and first.attempt == 1 and second.message_id != first.message_id
    assert any(e.type == 'run.started' for e in events)


@pytest.mark.asyncio
async def test_durable_result_survives_reopen_and_replay_is_original_snapshot(platform):
    from workbench_agent.persistence.backends.sqlite import SQLiteBackend
    from workbench_agent.application.results import ResultService

    p = EventPlatform(platform)
    run, guard, outcome = await p.finalizable_run()
    original = await p.results.finalize(guard, outcome, outcome.digest())
    await platform.backend.close()
    fresh = SQLiteBackend(platform.settings.storage.database_url.get_secret_value())
    try:
        assert await ResultService(fresh).finalize(guard, outcome, outcome.digest()) == original
        from workbench_agent.application.runs import RunService

        assert (await RunService(fresh).get(platform.auth, run.run_id)).outcome == original.outcome
    finally:
        await fresh.close()


@pytest.mark.asyncio
async def test_concurrent_finalization_commits_one_result_and_one_final_event(platform):
    import asyncio

    p = EventPlatform(platform)
    run, guard, outcome = await p.finalizable_run()
    results = await asyncio.gather(
        *(p.results.finalize(guard, outcome, outcome.digest()) for _ in range(3))
    )
    assert results[0] == results[1] == results[2]
    assert await platform.count_rows('run_outcomes') == 1
    assert len([e for e in await p.read_events(run.run_id) if e.type == 'run.completed']) == 1

from datetime import datetime, timezone
import pytest

from workbench_agent.domain.query_scope import (
    AlertScopeInput,
    resolve_alert_scope,
    revise_alert_scope,
)
from workbench_agent.runtime.graphs.baseline import BaselineObservation, evaluate_baseline


def input_values(**overrides):
    values = dict(
        team='payments',
        service='api',
        environment='test',
        body_region='ID',
        link_region='PH',
        anchor='2026-11-14T02:00:00Z',
        window_seconds=1800,
    )
    values.update(overrides)
    return AlertScopeInput(**values)


def test_scope_conflict_waits_and_preserves_fixed_anchor():
    alert = input_values()
    pending = resolve_alert_scope(alert)
    assert pending.required_fields == ('region',)
    assert pending.scope is None
    final = revise_alert_scope(alert, {'region': 'ID'})
    assert final.region == 'ID'
    assert final.anchor == datetime(2026, 11, 14, 2, tzinfo=timezone.utc)
    assert final.start == datetime(2026, 11, 14, 1, 30, tzinfo=timezone.utc)
    assert final.end == final.anchor


def test_verified_authoritative_region_resolves_conflict_and_unsupported_answer_fails():
    alert = input_values()
    assert resolve_alert_scope(alert, authoritative_region='SG').scope.region == 'SG'
    with pytest.raises(ValueError):
        revise_alert_scope(alert, {'region': 'SG', 'environment': 'live'})


def test_scope_rejects_naive_or_invalid_time():
    with pytest.raises(ValueError):
        resolve_alert_scope(input_values(anchor='2026-11-14T02:00:00'))
    with pytest.raises(ValueError):
        resolve_alert_scope(input_values(window_seconds=0))


def test_baseline_requires_valid_current_scoped_evidence_not_empty_or_unavailable():
    checks = [
        BaselineObservation('metrics', 'empty', None, 'scope-a'),
        BaselineObservation('logs', 'unavailable', None, 'scope-a'),
        BaselineObservation('trace', 'valid', 'ev-old', 'scope-old'),
    ]
    result = evaluate_baseline('scope-a', checks, required={'metrics', 'logs', 'trace'})
    assert result.complete is False
    assert result.business_normal is False
    assert result.statuses == {'metrics': 'empty', 'logs': 'unavailable', 'trace': 'stale'}
    assert result.evidence_refs == ()


@pytest.mark.asyncio
async def test_revision_preserves_anchor_window_and_rejects_stale_revision(platform):
    import json
    from workbench_agent.application.context import ContextResolver
    from workbench_agent.application.errors import DomainError

    resolver = ContextResolver(platform.backend)
    run = await platform.create_run()
    initial = {
        'region': 'ID',
        'service': 'api',
        'environment': 'test',
        'start_at': '2026-11-14T01:30:00+00:00',
        'end_at': '2026-11-14T02:00:00+00:00',
    }
    async with platform.backend.create_uow().begin_write() as uow:
        first = await resolver.query_scope(uow, platform.auth, run.run_id, initial)
    revised = await resolver.revise_scope(
        platform.auth, run.run_id, first['scope_id'], {'region': 'PH'}
    )
    assert json.loads(revised['scope_json']) == dict(initial, region='PH')
    with pytest.raises(DomainError) as exc:
        await resolver.revise_scope(platform.auth, run.run_id, first['scope_id'], {'region': 'SG'})
    assert exc.value.code == 'STALE_QUERY_SCOPE'
    with pytest.raises(DomainError):
        await resolver.revise_scope(
            platform.auth, run.run_id, revised['scope_id'], {'end_at': '2099-01-01T00:00:00Z'}
        )


def test_baseline_never_infers_normal_from_valid_retrieval_and_rejects_duplicate_steps():
    valid = [
        BaselineObservation('metrics', 'valid', 'e-m', 's'),
        BaselineObservation('logs', 'valid', 'e-l', 's'),
    ]
    result = evaluate_baseline('s', valid, required={'metrics', 'logs'})
    assert result.complete is True
    assert result.business_normal is False
    with pytest.raises(ValueError):
        evaluate_baseline('s', valid + [valid[0]], required={'metrics', 'logs'})


RCA_GRAPHS = ('rca.synthetic', 'rca.proposal', 'rca.batch_proposal')


@pytest.mark.parametrize('graph_id', RCA_GRAPHS)
async def test_conflicting_alert_resume_preserves_fixed_window(platform, graph_id):
    import json
    from support.rca_recovery import RcaRecovery

    p = RcaRecovery(platform, graph_id)
    run, waiting = await p.waiting_run()
    async with platform.backend.create_uow().begin_read() as uow:
        before = json.loads(
            (await platform.backend.runs.current_scope(uow, platform.auth.team_id, run.run_id))[
                'scope_json'
            ]
        )
    assert set(before) == {'service', 'environment', 'start_at', 'end_at'}
    await p.resume(run, waiting)
    async with platform.backend.create_uow().begin_read() as uow:
        accepted = json.loads(
            (await platform.backend.runs.current_scope(uow, platform.auth.team_id, run.run_id))[
                'scope_json'
            ]
        )
    assert accepted == {**before, 'region': 'id'}
    assert await p.worker.run_once() == 'projected'
    assert (await platform.get_run(run.run_id)).status == 'partial'
    async with platform.backend.create_uow().begin_read() as uow:
        final = json.loads(
            (await platform.backend.runs.current_scope(uow, platform.auth.team_id, run.run_id))[
                'scope_json'
            ]
        )
    assert final == accepted


@pytest.mark.parametrize('graph_id', RCA_GRAPHS)
@pytest.mark.parametrize('when', ['before_resume', 'after_resume'])
async def test_changed_scope_is_never_overwritten_by_resume(platform, graph_id, when):
    import json
    from support.rca_recovery import RcaRecovery
    from workbench_agent.application.context import ContextResolver
    from workbench_agent.application.errors import DomainError

    p = RcaRecovery(platform, graph_id)
    run, waiting = await p.waiting_run()
    if when == 'after_resume':
        await p.resume(run, waiting)
    changed = {'region': 'other', 'service': 'other'}
    async with platform.backend.create_uow().begin_write() as uow:
        await ContextResolver(platform.backend).query_scope(uow, platform.auth, run.run_id, changed)
    if when == 'before_resume':
        with pytest.raises(DomainError) as error:
            await p.resume(run, waiting)
        assert error.value.code == 'STALE_QUERY_SCOPE'
    else:
        assert await p.worker.run_once() == 'projected'
        assert (await platform.get_run(run.run_id)).status == 'partial'
    async with platform.backend.create_uow().begin_read() as uow:
        current = await platform.backend.runs.current_scope(uow, platform.auth.team_id, run.run_id)
    assert json.loads(current['scope_json']) == changed


@pytest.mark.parametrize('graph_id', RCA_GRAPHS)
async def test_unprojected_wait_is_recovered_after_worker_crash(platform, graph_id):
    from support.rca_recovery import RcaRecovery
    from support.jobs import expire_lease

    p = RcaRecovery(platform, graph_id)
    run, owned = await p.start()
    await expire_lease(platform, owned.guard)
    assert (await p.registry.reconciler.recover(run.run_id)).action == 'waiting'
    assert (await platform.get_run(run.run_id)).status == 'waiting_input'


@pytest.mark.parametrize('graph_id', RCA_GRAPHS)
async def test_nonconflicting_scope_stays_partial_without_baseline(platform, graph_id):
    from support.rca_recovery import RcaRecovery

    p = RcaRecovery(platform, graph_id)
    run, _ = await p.admit(conflict=False)
    assert await p.worker.run_once() == 'projected'
    view = await platform.get_run(run.run_id)
    assert view.status == 'partial'
    assert not any(item['status'] == 'normal' for item in view.outcome['findings'])
    assert view.outcome['unresolved_items']

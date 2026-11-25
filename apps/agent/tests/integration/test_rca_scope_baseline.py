from datetime import datetime, timezone
import pytest

from workbench_agent.domain.query_scope import (
    AlertScopeInput,
    resolve_alert_scope,
    revise_alert_scope,
)


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


RCA_GRAPHS = ('rca.synthetic', 'rca.proposal', 'rca.batch_proposal')


@pytest.mark.parametrize('graph_id', RCA_GRAPHS)
async def test_scope_resume_rejects_cross_team_and_time_changes_and_replays_once(
    platform, graph_id
):
    from workbench_agent.application.errors import DomainError
    from workbench_agent.application.runs import RunService
    from workbench_agent.domain.runs import ResumeRun
    from support.rca_recovery import RcaRecovery

    recovery = RcaRecovery(platform, graph_id)
    run, waiting = await recovery.waiting_run()
    request = {
        'interrupt_id': waiting['interrupt_id'],
        'expected_revision': waiting['expected_revision'],
        'response': {'kind': 'user_input', 'values': {'region': 'id'}},
    }
    service = RunService(platform.backend)
    with pytest.raises(DomainError) as error:
        await service.resume(
            platform.other_auth, run.run_id, ResumeRun.model_validate(request), 'foreign'
        )
    assert error.value.status == 404
    altered = {
        **request,
        'response': {
            'kind': 'user_input',
            'values': {'region': 'id', 'end_at': '2099-01-01T00:00:00Z'},
        },
    }
    with pytest.raises(DomainError) as error:
        await service.resume(
            platform.auth, run.run_id, ResumeRun.model_validate(altered), 'invalid'
        )
    assert error.value.code == 'INVALID_RESPONSE'
    accepted = await service.resume(
        platform.auth, run.run_id, ResumeRun.model_validate(request), 'answer'
    )
    replayed = await service.resume(
        platform.auth, run.run_id, ResumeRun.model_validate(request), 'answer'
    )
    assert replayed.replayed and accepted.run_id == replayed.run_id
    assert await platform.count_rows('resume_requests') == 1


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

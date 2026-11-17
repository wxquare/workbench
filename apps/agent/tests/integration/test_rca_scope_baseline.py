from datetime import datetime, timezone
import pytest

from workbench_agent.domain.query_scope import AlertScopeInput, resolve_alert_scope, revise_alert_scope
from workbench_agent.runtime.graphs.baseline import BaselineObservation, evaluate_baseline


def input_values(**overrides):
    values = dict(team='payments', service='api', environment='test',
                  body_region='ID', link_region='PH', anchor='2026-11-14T02:00:00Z',
                  window_seconds=1800)
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
    checks = [BaselineObservation('metrics', 'empty', None, 'scope-a'),
              BaselineObservation('logs', 'unavailable', None, 'scope-a'),
              BaselineObservation('trace', 'valid', 'ev-old', 'scope-old')]
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
    initial = {'region':'ID', 'service':'api', 'environment':'test',
               'start_at':'2026-11-14T01:30:00+00:00', 'end_at':'2026-11-14T02:00:00+00:00'}
    async with platform.backend.create_uow().begin_write() as uow:
        first = await resolver.query_scope(uow, platform.auth, run.run_id, initial)
    revised = await resolver.revise_scope(platform.auth, run.run_id, first['scope_id'], {'region':'PH'})
    assert json.loads(revised['scope_json']) == dict(initial, region='PH')
    with pytest.raises(DomainError) as exc:
        await resolver.revise_scope(platform.auth, run.run_id, first['scope_id'], {'region':'SG'})
    assert exc.value.code == 'STALE_QUERY_SCOPE'
    with pytest.raises(DomainError):
        await resolver.revise_scope(platform.auth, run.run_id, revised['scope_id'], {'end_at':'2099-01-01T00:00:00Z'})


@pytest.mark.asyncio
@pytest.mark.parametrize('version', ['dod.v9', 'dod.v10'])
async def test_conflicting_alert_waits_then_preserves_window_in_same_run(platform, client, version):
    from test_m2_graph import publish_default
    from workbench_agent.cli.worker import run_worker
    from support.runs import make_run_payload
    await publish_default(platform, client, version)
    payload = make_run_payload(platform.agent_id, mode='rca')
    payload['business_context'] = {'body_region':'ID', 'link_region':'PH', 'service':'api',
                                   'environment':'test', 'anchor':'2026-11-14T02:00:00Z',
                                   'window_seconds':1800}
    created = await client.post('/v1/runs', json=payload, headers={'Idempotency-Key':'rca-v9'})
    assert created.status_code == 202, created.text
    run_id = created.json()['run_id']
    assert (await run_worker(platform.settings, once=True))['status'] == 'projected'
    view = (await client.get('/v1/runs/' + run_id)).json()
    assert view['status'] == 'waiting_input'
    waiting = view['waiting']
    response = {'interrupt_id':waiting['interrupt_id'], 'expected_revision':view['revision'],
                'response':{'kind':'user_input', 'values':{'region':'ID'}, 'text':None}}
    resumed = await client.post('/v1/runs/' + run_id + '/resume', json=response,
                                headers={'Idempotency-Key':'rca-v9-resume'})
    assert resumed.status_code == 202, resumed.text
    assert resumed.json()['run_id'] == run_id
    # The accepted resume commits the chosen region and original time window atomically.
    async with platform.backend.create_uow().begin_read() as uow:
        accepted_scope = await platform.backend.runs.current_scope(uow, platform.auth.team_id, run_id)
    import json
    assert json.loads(accepted_scope['scope_json']) == {'region':'ID', 'service':'api',
        'environment':'test', 'start_at':'2026-11-14T01:30:00+00:00', 'end_at':'2026-11-14T02:00:00+00:00'}
    assert (await run_worker(platform.settings, once=True))['status'] == 'projected'
    view = (await client.get('/v1/runs/' + run_id)).json()
    assert view['status'] == 'partial'
    async with platform.backend.create_uow().begin_read() as uow:
        scope = await platform.backend.runs.current_scope(uow, platform.auth.team_id, run_id)
    import json
    assert json.loads(scope['scope_json']) == {'region':'ID', 'service':'api', 'environment':'test',
        'start_at':'2026-11-14T01:30:00+00:00', 'end_at':'2026-11-14T02:00:00+00:00'}


@pytest.mark.asyncio
@pytest.mark.parametrize('version', ['dod.v9', 'dod.v10'])
async def test_newer_scope_is_not_overwritten_after_resume(platform, client, version):
    from test_m2_graph import publish_default
    from workbench_agent.cli.worker import run_worker
    from support.runs import make_run_payload
    from workbench_agent.application.context import ContextResolver
    import json
    await publish_default(platform, client, version)
    payload = make_run_payload(platform.agent_id, mode='rca')
    payload['business_context'] = {'body_region':'ID', 'link_region':'PH', 'service':'api',
        'environment':'test', 'anchor':'2026-11-14T02:00:00Z', 'window_seconds':1800}
    created = await client.post('/v1/runs', json=payload, headers={'Idempotency-Key':'race-v9'})
    run_id = created.json()['run_id']
    await run_worker(platform.settings, once=True)
    view = (await client.get('/v1/runs/' + run_id)).json()
    response = {'interrupt_id':view['waiting']['interrupt_id'], 'expected_revision':view['revision'],
                'response':{'kind':'user_input', 'values':{'region':'ID'}, 'text':None}}
    assert (await client.post('/v1/runs/' + run_id + '/resume', json=response,
                              headers={'Idempotency-Key':'race-v9-resume'})).status_code == 202
    other = {'region':'PH', 'service':'other'}
    async with platform.backend.create_uow().begin_write() as uow:
        await ContextResolver(platform.backend).query_scope(uow, platform.auth, run_id, other)
    assert (await run_worker(platform.settings, once=True))['status'] == 'projected'
    result = (await client.get('/v1/runs/' + run_id)).json()
    assert result['status'] == 'partial'
    async with platform.backend.create_uow().begin_read() as uow:
        current = await platform.backend.runs.current_scope(uow, platform.auth.team_id, run_id)
    assert json.loads(current['scope_json']) == other


@pytest.mark.asyncio
@pytest.mark.parametrize('version', ['dod.v9', 'dod.v10'])
async def test_recovery_projects_v9_wait_after_worker_crash(platform, client, version):
    from test_m2_graph import publish_default
    from support.runs import make_run_payload
    from support.jobs import claim, expire_lease
    from workbench_agent.runtime.graphs.registry import foundation_worker_registry
    from workbench_agent.jobs.worker import Execution
    await publish_default(platform, client, version)
    payload = make_run_payload(platform.agent_id, mode='rca')
    payload['business_context'] = {'body_region':'ID', 'link_region':'PH', 'service':'api',
        'environment':'test', 'anchor':'2026-11-14T02:00:00Z', 'window_seconds':1800}
    created = await client.post('/v1/runs', json=payload, headers={'Idempotency-Key':'crash-v9'})
    run_id = created.json()['run_id']
    registry = foundation_worker_registry(platform.backend)
    owned = await claim(platform)
    await registry._executors[version].execute(Execution(platform.backend, owned))
    await expire_lease(platform, owned.guard)
    decision = await registry.reconciler.recover(run_id)
    assert decision.action == 'waiting'
    assert (await platform.get_run(run_id)).status == 'waiting_input'


def test_baseline_never_infers_normal_from_valid_retrieval_and_rejects_duplicate_steps():
    valid = [BaselineObservation('metrics', 'valid', 'e-m', 's'),
             BaselineObservation('logs', 'valid', 'e-l', 's')]
    result = evaluate_baseline('s', valid, required={'metrics', 'logs'})
    assert result.complete is True
    assert result.business_normal is False
    with pytest.raises(ValueError):
        evaluate_baseline('s', valid + [valid[0]], required={'metrics', 'logs'})


@pytest.mark.asyncio
async def test_nonconflicting_scope_stays_partial_without_baseline(platform, client):
    from test_m2_graph import publish_default
    from workbench_agent.cli.worker import run_worker
    from support.runs import make_run_payload
    await publish_default(platform, client, 'dod.v9')
    payload = make_run_payload(platform.agent_id, mode='rca')
    payload['business_context'] = {'region':'ID', 'service':'api', 'environment':'test',
        'anchor':'2026-11-14T02:00:00Z', 'window_seconds':1800}
    created = await client.post('/v1/runs', json=payload, headers={'Idempotency-Key':'simple-v9'})
    run_id = created.json()['run_id']
    assert (await run_worker(platform.settings, once=True))['status'] == 'projected'
    view = (await client.get('/v1/runs/' + run_id)).json()
    assert view['status'] == 'partial'
    assert not any(item['status'] == 'normal' for item in view['outcome']['findings'])
    assert view['outcome']['unresolved_items']


@pytest.mark.asyncio
@pytest.mark.parametrize('version', ['dod.v9', 'dod.v10'])
async def test_scope_revision_changed_while_waiting_is_not_overwritten(platform, client, version):
    from test_m2_graph import publish_default
    from workbench_agent.cli.worker import run_worker
    from support.runs import make_run_payload
    from workbench_agent.application.context import ContextResolver
    import json
    await publish_default(platform, client, version)
    payload = make_run_payload(platform.agent_id, mode='rca')
    payload['business_context'] = {'body_region':'ID', 'link_region':'PH', 'service':'api',
        'environment':'test', 'anchor':'2026-11-14T02:00:00Z', 'window_seconds':1800}
    created = await client.post('/v1/runs', json=payload, headers={'Idempotency-Key':'wait-race-v9'})
    run_id = created.json()['run_id']
    assert (await run_worker(platform.settings, once=True))['status'] == 'projected'
    view = (await client.get('/v1/runs/' + run_id)).json()
    assert view['status'] == 'waiting_input'
    async with platform.backend.create_uow().begin_write() as uow:
        before = await platform.backend.runs.current_scope(uow, platform.auth.team_id, run_id)
        assert json.loads(before['scope_json']) == {'service':'api', 'environment':'test',
            'start_at':'2026-11-14T01:30:00+00:00', 'end_at':'2026-11-14T02:00:00+00:00'}
        changed = {'region':'SG', 'service':'other', 'environment':'test',
                   'start_at':'2026-11-14T00:00:00+00:00', 'end_at':'2026-11-14T00:30:00+00:00'}
        await ContextResolver(platform.backend).query_scope(uow, platform.auth, run_id, changed)
    response = {'interrupt_id':view['waiting']['interrupt_id'], 'expected_revision':view['revision'],
                'response':{'kind':'user_input', 'values':{'region':'ID'}, 'text':None}}
    resumed = await client.post('/v1/runs/' + run_id + '/resume', json=response,
                                headers={'Idempotency-Key':'wait-race-v9-resume'})
    assert resumed.status_code == 409
    assert resumed.json()['code'] == 'STALE_QUERY_SCOPE'
    async with platform.backend.create_uow().begin_read() as uow:
        after = await platform.backend.runs.current_scope(uow, platform.auth.team_id, run_id)
    assert json.loads(after['scope_json']) == changed

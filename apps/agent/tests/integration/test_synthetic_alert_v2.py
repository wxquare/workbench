import hashlib
import hmac
import json
import time

import pytest
from sqlalchemy import select
from workbench_agent.application.errors import DomainError
from workbench_agent.ingress.alerts import mac_message


def signed(event):
    raw = json.dumps(event, sort_keys=True).encode()
    timestamp = str(int(time.time()))
    signature = hmac.new(
        b'test-only-secret',
        mac_message('synthetic-alert-v2', 'v2-test', timestamp, raw),
        hashlib.sha256,
    ).hexdigest()
    return raw, {'x-dod-timestamp': timestamp, 'x-dod-signature': signature}


def event():
    from datetime import datetime, timezone

    now = int(time.time())
    return {
        'event_id': 'alert-2',
        'text': 'fictional test alert',
        'region': 'id',
        'service': 'checkout',
        'environment': 'test',
        'rule': 'error-rate',
        'anchor': datetime.fromtimestamp(now, timezone.utc).isoformat(),
        'window_seconds': 600,
        'occurred_at': now,
    }


def ingress(platform):
    from workbench_agent.ingress.alerts import SyntheticAlertV2Binding, SyntheticAlertV2Ingress

    return SyntheticAlertV2Ingress(
        platform.backend,
        SyntheticAlertV2Binding(
            connector_id='v2-test',
            team_id=platform.auth.team_id,
            agent_id=platform.agent_id,
            actor_subject=platform.auth.subject,
            secret=b'test-only-secret',
            identity_id='v2-test',
            allowed_scopes=frozenset({('checkout', 'test', 'id', 'error-rate')}),
        ),
    )


async def publish_v13(platform, *, with_policy=True):
    from workbench_agent.domain.profiles import ProfileDraft
    from uuid import uuid4

    context = await platform.contexts.resolve(platform.auth, platform.agent_id)
    agent = await platform.profiles.get_agent(platform.auth, platform.agent_id)
    draft = await platform.profiles.save_draft(
        platform.auth,
        platform.agent_id,
        ProfileDraft(default_model=context.default_model, graph_id='rca.synthetic'),
        agent['revision'],
        key=str(uuid4()),
    )
    await platform.profiles.publish(
        platform.auth, platform.agent_id, draft['revision'], key=str(uuid4())
    )
    if with_policy:
        from workbench_agent.application.synthetic_source_policy import SyntheticSourcePolicy

        await SyntheticSourcePolicy(platform.backend).sync(
            [
                dict(
                    team_id=platform.auth.team_id,
                    agent_id=platform.agent_id,
                    connector_id='v2-test',
                    identity_id='v2-test',
                    subject=platform.auth.subject,
                    service='checkout',
                    environment='test',
                    region='id',
                    rule='error-rate',
                )
            ],
            connector_identities={
                'v2-test': {
                    'subject': platform.auth.subject,
                    'team_id': platform.auth.team_id,
                    'agent_ids': [platform.agent_id],
                }
            },
            expected_revision=0,
        )


@pytest.mark.asyncio
async def test_v2_source_is_bound_atomically_and_replayed(platform):
    from workbench_agent.persistence import schema

    await publish_v13(platform)
    service = ingress(platform)
    raw, headers = signed(event())
    first = await service.accept(raw, headers)
    replay = await service.accept(raw, headers)
    assert first.run_id == replay.run_id
    async with platform.backend.create_uow().begin_read() as uow:
        rows = (await uow.execute(select(schema.synthetic_alert_sources))).mappings().all()
    assert len(rows) == 1 and rows[0]['run_id'] == first.run_id
    assert json.loads(rows[0]['payload'])['service'] == 'checkout'


@pytest.mark.asyncio
async def test_v2_rejects_unauthorized_scope_and_tampered_signature(platform):
    service = ingress(platform)
    denied = event()
    denied['service'] = 'other'
    with pytest.raises(DomainError):
        await service.accept(*signed(denied))
    raw, headers = signed(event())
    with pytest.raises(DomainError):
        await service.accept(raw + b' ', headers)
    assert await platform.count_rows('runs') == 0


@pytest.mark.asyncio
async def test_v2_worker_projects_structured_incident_without_model_text(platform):
    from workbench_agent.persistence import schema
    from workbench_agent.jobs.worker import Worker, DeploymentConnectorPolicy
    from workbench_agent.runtime.graphs.registry import foundation_worker_registry

    await publish_v13(platform)
    source = event()
    accepted = await ingress(platform).accept(*signed(source))
    policy = DeploymentConnectorPolicy(
        lambda: {
            'v2-test': {
                'subject': platform.auth.subject,
                'team_id': platform.auth.team_id,
                'agent_ids': [platform.agent_id],
            }
        }
    )
    worker = Worker(
        platform.backend,
        'test-v2',
        foundation_worker_registry(platform.backend),
        connector_policy=policy,
    )
    assert await worker.run_once() == 'projected'
    async with platform.backend.create_uow().begin_read() as uow:
        rows = (
            (
                await uow.execute(
                    select(schema.incidents).where(
                        schema.incidents.c.team_id == platform.auth.team_id,
                        schema.incidents.c.run_id == str(accepted.run_id),
                    )
                )
            )
            .mappings()
            .all()
        )
    assert len(rows) == 1
    assert rows[0]['source'] == 'synthetic-alert-v2' and rows[0]['service'] == 'checkout'
    assert json.loads(rows[0]['conclusion']) == {'rule': 'error-rate', 'status': 'alert_observed'}
    assert rows[0]['severity'] == 'unknown'


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['projection_crash', 'tampered_source'])
async def test_v2_projection_failure_never_commits_incident_or_result(
    platform, monkeypatch, failure
):
    from workbench_agent.persistence import schema
    from sqlalchemy import update, func
    from workbench_agent.jobs.worker import Worker, DeploymentConnectorPolicy
    from workbench_agent.runtime.graphs.registry import foundation_worker_registry

    await publish_v13(platform)
    accepted = await ingress(platform).accept(*signed(event()))
    if failure == 'tampered_source':
        async with platform.backend.create_uow().begin_write() as uow:
            await uow.execute(
                update(schema.synthetic_alert_sources)
                .where(schema.synthetic_alert_sources.c.run_id == str(accepted.run_id))
                .values(payload='{}')
            )
    else:
        import workbench_agent.incidents.synthetic as projection

        original = projection.project_incident

        async def crash(backend, uow, guard, run, now):
            if failure == 'changed_scope':
                scope = await backend.runs.current_scope(uow, guard.team_id, guard.run_id)
                data = json.loads(scope['scope_json'])
                data['service'] = 'other'
                await uow.execute(
                    update(schema.query_scope_revisions)
                    .where(schema.query_scope_revisions.c.scope_id == scope['scope_id'])
                    .values(scope_json=json.dumps(data))
                )
            await original(backend, uow, guard, run, now)
            raise RuntimeError('injected crash')

        monkeypatch.setattr(projection, 'project_incident', crash)
    policy = DeploymentConnectorPolicy(
        lambda: {
            'v2-test': {
                'subject': platform.auth.subject,
                'team_id': platform.auth.team_id,
                'agent_ids': [platform.agent_id],
            }
        }
    )
    worker = Worker(
        platform.backend,
        'test-v2-failure',
        foundation_worker_registry(platform.backend),
        connector_policy=policy,
    )
    assert await worker.run_once() != 'projected'
    async with platform.backend.create_uow().begin_read() as uow:
        for table in (schema.incidents, schema.run_outcomes, schema.synthetic_result_bindings):
            assert (
                await uow.execute(
                    select(func.count())
                    .select_from(table)
                    .where(table.c.run_id == str(accepted.run_id))
                )
            ).scalar_one() == 0


@pytest.mark.asyncio
async def test_v2_admission_rolls_back_when_source_write_fails(platform, monkeypatch):
    await publish_v13(platform)
    service = ingress(platform)
    original = service.runs.create

    async def interrupted(*args, **kwargs):
        await original(*args, **kwargs)
        raise RuntimeError('source write failed')

    monkeypatch.setattr(service.runs, 'create', interrupted)
    with pytest.raises(RuntimeError, match='source write failed'):
        await service.accept(*signed(event()))
    assert await platform.count_rows('runs') == 0
    assert await platform.count_rows('source_events') == 0
    assert await platform.count_rows('synthetic_alert_sources') == 0


@pytest.mark.asyncio
async def test_v2_source_cannot_reference_another_tenants_run(platform):
    from sqlalchemy import insert
    from workbench_agent.persistence import schema
    from workbench_agent.persistence.errors import StorageError

    await publish_v13(platform)
    accepted = await ingress(platform).accept(*signed(event()))
    with pytest.raises(StorageError):
        async with platform.backend.create_uow().begin_write() as uow:
            await uow.execute(
                insert(schema.synthetic_alert_sources).values(
                    team_id=platform.other_auth.team_id,
                    run_id=str(accepted.run_id),
                    connector_id='other',
                    payload='{}',
                    payload_hash='0' * 64,
                    created_at=0,
                )
            )


@pytest.mark.asyncio
async def test_v13_rejects_ordinary_api_and_v1_ingress_before_run_admission(platform):
    from workbench_agent.application.runs import RunService
    from workbench_agent.domain.runs import CreateRun
    from workbench_agent.ingress.alerts import SyntheticAlertIngress, SyntheticAlertBinding

    await publish_v13(platform)
    request = CreateRun(agent_id=platform.agent_id, mode='rca', input={'text': 'ordinary'})
    with pytest.raises(DomainError):
        await RunService(platform.backend).create(platform.auth, request, 'ordinary')
    v1 = SyntheticAlertIngress(
        platform.backend,
        SyntheticAlertBinding(
            connector_id='v2-test',
            team_id=platform.auth.team_id,
            agent_id=platform.agent_id,
            actor_subject=platform.auth.subject,
            secret=b'test-only-secret',
        ),
    )
    payload = {
        'event_id': 'legacy',
        'text': 'test',
        'region': 'id',
        'occurred_at': int(time.time()),
    }
    raw = json.dumps(payload).encode()
    timestamp = str(int(time.time()))
    signature = hmac.new(
        b'test-only-secret',
        mac_message('synthetic-alert-v1', 'v2-test', timestamp, raw),
        hashlib.sha256,
    ).hexdigest()
    with pytest.raises(DomainError):
        await v1.accept(raw, {'x-dod-timestamp': timestamp, 'x-dod-signature': signature})
    assert await platform.count_rows('runs') == 0


@pytest.mark.asyncio
async def test_scope_change_produces_safe_partial_without_incident(platform, monkeypatch):
    from workbench_agent.persistence import schema
    from sqlalchemy import update
    from workbench_agent.jobs.worker import Worker, DeploymentConnectorPolicy
    from workbench_agent.runtime.graphs.registry import foundation_worker_registry
    from workbench_agent.application.results import ResultService

    await publish_v13(platform)
    accepted = await ingress(platform).accept(*signed(event()))
    original = ResultService.finalize_in

    async def change_scope(self, uow, guard, outcome, digest):
        scope = await self.backend.runs.current_scope(uow, guard.team_id, guard.run_id)
        changed = json.loads(scope['scope_json'])
        changed['service'] = 'another-service'
        await uow.execute(
            update(schema.query_scope_revisions)
            .where(schema.query_scope_revisions.c.scope_id == scope['scope_id'])
            .values(scope_json=json.dumps(changed))
        )
        return await original(self, uow, guard, outcome, digest)

    monkeypatch.setattr(ResultService, 'finalize_in', change_scope)
    policy = DeploymentConnectorPolicy(
        lambda: {
            'v2-test': {
                'subject': platform.auth.subject,
                'team_id': platform.auth.team_id,
                'agent_ids': [platform.agent_id],
            }
        }
    )
    assert (
        await Worker(
            platform.backend,
            'changed-scope',
            foundation_worker_registry(platform.backend),
            connector_policy=policy,
        ).run_once()
        == 'projected'
    )
    view = await platform.get_run(accepted.run_id)
    assert view.status == 'partial' and not view.outcome['evidence_refs']
    assert await platform.count_rows('incidents') == 0


@pytest.mark.asyncio
async def test_deadline_before_scope_yields_safe_partial(platform):
    from sqlalchemy import update
    from workbench_agent.persistence import schema
    from support.jobs import claim
    from support.events import save_outcome
    from workbench_agent.domain.outcomes import RunOutcome, UsageSummary
    from workbench_agent.jobs.deadlines import DeadlineService

    await publish_v13(platform)
    accepted = await ingress(platform).accept(*signed(event()))
    owned = await claim(platform)
    outcome = RunOutcome(
        kind='analysis.v1',
        completed=False,
        summary='untrusted prior summary',
        findings=(),
        root_cause_hypotheses=(),
        impact=None,
        evidence_refs=(),
        unresolved_items=('pending',),
        proposed_actions=(),
        knowledge_gaps=(),
        usage_summary=UsageSummary(),
    )
    await save_outcome(platform, owned.guard, outcome.model_dump(mode='json'))
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(
            update(schema.runs)
            .where(schema.runs.c.run_id == str(accepted.run_id))
            .values(deadline_at=0)
        )
    from workbench_agent.jobs.worker import DeploymentConnectorPolicy

    policy = DeploymentConnectorPolicy(
        lambda: {
            'v2-test': {
                'subject': platform.auth.subject,
                'team_id': platform.auth.team_id,
                'agent_ids': [platform.agent_id],
            }
        }
    )
    assert await DeadlineService(platform.backend, connector_policy=policy).expire_owned(
        owned.guard
    )
    view = await platform.get_run(accepted.run_id)
    assert view.status == 'partial' and view.outcome['summary'] != 'untrusted prior summary'
    assert await platform.count_rows('incidents') == 0
    assert await platform.count_rows('synthetic_result_bindings') == 1


@pytest.mark.asyncio
async def test_reconciler_takeover_after_scope_change_yields_safe_partial(platform):
    from sqlalchemy import update
    from workbench_agent.persistence import schema
    from support.jobs import claim, expire_lease
    from workbench_agent.jobs.worker import Execution, DeploymentConnectorPolicy
    from workbench_agent.jobs.reconciler import Reconciler
    from workbench_agent.runtime.graphs.registry import foundation_worker_registry

    await publish_v13(platform)
    accepted = await ingress(platform).accept(*signed(event()))
    owned = await claim(platform)
    policy = DeploymentConnectorPolicy(
        lambda: {
            'v2-test': {
                'subject': platform.auth.subject,
                'team_id': platform.auth.team_id,
                'agent_ids': [platform.agent_id],
            }
        }
    )
    registry = foundation_worker_registry(platform.backend)
    await registry._executors['rca.synthetic'].execute(Execution(platform.backend, owned, policy))
    async with platform.backend.create_uow().begin_write() as uow:
        scope = await platform.backend.runs.current_scope(
            uow, owned.guard.team_id, owned.guard.run_id
        )
        changed = json.loads(scope['scope_json'])
        changed['region'] = 'different'
        await uow.execute(
            update(schema.query_scope_revisions)
            .where(schema.query_scope_revisions.c.scope_id == scope['scope_id'])
            .values(scope_json=json.dumps(changed))
        )
    await expire_lease(platform, owned.guard)
    reconciler = Reconciler(platform.backend, registry)
    reconciler.connector_policy = policy
    assert (await reconciler.recover(str(accepted.run_id))).action == 'finalized'
    view = await platform.get_run(accepted.run_id)
    assert view.status == 'partial' and view.outcome['summary'].startswith('Analysis is incomplete')
    assert await platform.count_rows('incidents') == 0
    assert await platform.count_rows('synthetic_result_bindings') == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['qa', 'rca', 'auto'])
async def test_v13_http_admission_rejects_untrusted_sources(client, platform, mode):
    await publish_v13(platform)
    response = await client.post(
        '/v1/runs',
        headers={'Idempotency-Key': 'http-' + mode},
        json={
            'agent_id': platform.agent_id,
            'mode': mode,
            'input': {'type': 'text', 'text': 'ordinary'},
            'business_context': {'source': 'synthetic-alert-v2'},
        },
    )
    assert response.status_code == 409
    assert await platform.count_rows('runs') == 0


@pytest.mark.asyncio
async def test_safe_partial_replay_requires_identical_original_outcome(platform, monkeypatch):
    from workbench_agent.persistence import schema
    from sqlalchemy import update
    from workbench_agent.jobs.worker import Worker, DeploymentConnectorPolicy
    from workbench_agent.runtime.graphs.registry import foundation_worker_registry
    from workbench_agent.application.results import ResultService

    await publish_v13(platform)
    accepted = await ingress(platform).accept(*signed(event()))
    original = ResultService.finalize_in
    captured = []

    async def change_scope(self, uow, guard, outcome, digest):
        captured.append((guard, outcome))
        scope = await self.backend.runs.current_scope(uow, guard.team_id, guard.run_id)
        changed = json.loads(scope['scope_json'])
        changed['service'] = 'different'
        await uow.execute(
            update(schema.query_scope_revisions)
            .where(schema.query_scope_revisions.c.scope_id == scope['scope_id'])
            .values(scope_json=json.dumps(changed))
        )
        return await original(self, uow, guard, outcome, digest)

    monkeypatch.setattr(ResultService, 'finalize_in', change_scope)
    policy = DeploymentConnectorPolicy(
        lambda: {
            'v2-test': {
                'subject': platform.auth.subject,
                'team_id': platform.auth.team_id,
                'agent_ids': [platform.agent_id],
            }
        }
    )
    assert (
        await Worker(
            platform.backend,
            'safe-replay',
            foundation_worker_registry(platform.backend),
            connector_policy=policy,
        ).run_once()
        == 'projected'
    )
    monkeypatch.setattr(ResultService, 'finalize_in', original)
    guard, outcome = captured[0]
    assert (
        await ResultService(platform.backend).finalize(guard, outcome, outcome.digest())
    ).status == 'partial'
    changed = outcome.model_copy(update={'summary': 'Different original sensitive content'})
    with pytest.raises(DomainError, match='RESULT_CONFLICT'):
        await ResultService(platform.backend).finalize(guard, changed, changed.digest())
    # A historical v13 result with no binding must never accept ambiguous replay.
    from sqlalchemy import delete

    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(
            delete(schema.synthetic_result_bindings).where(
                schema.synthetic_result_bindings.c.run_id == str(accepted.run_id)
            )
        )
    with pytest.raises(DomainError, match='RESULT_CONFLICT'):
        await ResultService(platform.backend).finalize(guard, outcome, outcome.digest())


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['worker', 'deadline', 'reconciler'])
async def test_candidate_digest_precedes_permission_sanitization(platform, monkeypatch, path):
    from uuid import uuid4
    from sqlalchemy import update, select
    from workbench_agent.persistence import schema
    from workbench_agent.domain.outcomes import RunOutcome, UsageSummary
    from workbench_agent.application.results import ResultService
    from workbench_agent.jobs.worker import Worker, Execution, DeploymentConnectorPolicy
    from workbench_agent.jobs.deadlines import DeadlineService
    from workbench_agent.jobs.reconciler import Reconciler
    from workbench_agent.runtime.graphs.registry import foundation_worker_registry
    from support.jobs import claim, expire_lease
    from support.events import save_outcome
    import workbench_agent.runtime.results as runtime_results
    import workbench_agent.jobs.reconciler as recovery

    await publish_v13(platform)
    accepted = await ingress(platform).accept(*signed(event()))
    outcome = RunOutcome(
        kind='analysis.v1',
        completed=False,
        summary='sensitive original alpha',
        findings=(),
        root_cause_hypotheses=(),
        impact=None,
        evidence_refs=(uuid4(),),
        unresolved_items=('pending',),
        proposed_actions=(),
        knowledge_gaps=(),
        usage_summary=UsageSummary(),
    )
    policy = DeploymentConnectorPolicy(
        lambda: {
            'v2-test': {
                'subject': platform.auth.subject,
                'team_id': platform.auth.team_id,
                'agent_ids': [platform.agent_id],
            }
        }
    )
    registry = foundation_worker_registry(platform.backend)
    original = ResultService.finalize_in
    guards = []

    async def capture(self, uow, guard, value, digest):
        guards.append(guard)
        return await original(self, uow, guard, value, digest)

    monkeypatch.setattr(ResultService, 'finalize_in', capture)
    if path == 'worker':
        monkeypatch.setattr(runtime_results, 'saved_outcome', lambda saved, version: outcome)
        assert (
            await Worker(
                platform.backend, 'digest-worker', registry, connector_policy=policy
            ).run_once()
            == 'projected'
        )
    elif path == 'deadline':
        owned = await claim(platform)
        await save_outcome(platform, owned.guard, outcome.model_dump(mode='json'))
        async with platform.backend.create_uow().begin_write() as uow:
            await uow.execute(
                update(schema.runs)
                .where(schema.runs.c.run_id == str(accepted.run_id))
                .values(deadline_at=0)
            )
        assert await DeadlineService(platform.backend, connector_policy=policy).expire_owned(
            owned.guard
        )
    else:
        owned = await claim(platform)
        await registry._executors['rca.synthetic'].execute(
            Execution(platform.backend, owned, policy)
        )
        await expire_lease(platform, owned.guard)
        monkeypatch.setattr(recovery, 'saved_outcome', lambda saved, version: outcome)
        reconciler = Reconciler(platform.backend, registry)
        reconciler.connector_policy = policy
        assert (await reconciler.recover(str(accepted.run_id))).action == 'finalized'
    monkeypatch.setattr(ResultService, 'finalize_in', original)
    service = ResultService(platform.backend)
    guard = guards[0]
    assert (await service.finalize(guard, outcome, outcome.digest())).status == 'partial'
    changed = outcome.model_copy(update={'summary': 'sensitive original beta'})
    with pytest.raises(DomainError, match='RESULT_CONFLICT'):
        await service.finalize(guard, changed, changed.digest())
    async with platform.backend.create_uow().begin_read() as uow:
        result = (
            (
                await uow.execute(
                    select(schema.run_outcomes).where(
                        schema.run_outcomes.c.run_id == str(accepted.run_id)
                    )
                )
            )
            .mappings()
            .one()
        )
        binding = (
            (
                await uow.execute(
                    select(schema.synthetic_result_bindings).where(
                        schema.synthetic_result_bindings.c.run_id == str(accepted.run_id)
                    )
                )
            )
            .mappings()
            .one()
        )
    assert binding['candidate_digest'] == outcome.digest()
    assert 'sensitive original alpha' not in result['payload']


@pytest.mark.asyncio
async def test_worker_cannot_borrow_another_connector_identity(platform):
    from workbench_agent.jobs.worker import Worker, DeploymentConnectorPolicy
    from workbench_agent.runtime.graphs.registry import foundation_worker_registry

    await publish_v13(platform)
    await ingress(platform).accept(*signed(event()))
    policy = DeploymentConnectorPolicy(
        lambda: {
            'different-identity': {
                'subject': platform.auth.subject,
                'team_id': platform.auth.team_id,
                'agent_ids': [platform.agent_id],
            }
        }
    )
    assert (
        await Worker(
            platform.backend,
            'identity-check',
            foundation_worker_registry(platform.backend),
            connector_policy=policy,
        ).run_once()
        == 'capability_blocked'
    )
    assert await platform.count_rows('incidents') == 0


@pytest.mark.asyncio
async def test_deadline_does_not_project_after_precise_connector_revocation(platform):
    from sqlalchemy import update
    from workbench_agent.persistence import schema
    from support.jobs import claim
    from workbench_agent.jobs.worker import Execution, DeploymentConnectorPolicy
    from workbench_agent.jobs.deadlines import DeadlineService
    from workbench_agent.runtime.graphs.registry import foundation_worker_registry

    await publish_v13(platform)
    accepted = await ingress(platform).accept(*signed(event()))
    identities = {
        'v2-test': {
            'subject': platform.auth.subject,
            'team_id': platform.auth.team_id,
            'agent_ids': [platform.agent_id],
        },
        'other': {
            'subject': platform.auth.subject,
            'team_id': platform.auth.team_id,
            'agent_ids': [platform.agent_id],
        },
    }
    policy = DeploymentConnectorPolicy(lambda: identities)
    owned = await claim(platform)
    await (
        foundation_worker_registry(platform.backend)
        ._executors['rca.synthetic']
        .execute(Execution(platform.backend, owned, policy))
    )
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(
            update(schema.runs)
            .where(schema.runs.c.run_id == str(accepted.run_id))
            .values(deadline_at=0)
        )
    identities.pop('v2-test')
    deadlines = DeadlineService(platform.backend)
    deadlines.connector_policy = policy
    assert await deadlines.expire_owned(owned.guard)
    view = await platform.get_run(accepted.run_id)
    assert view.status == 'timed_out' and view.outcome is None
    assert await platform.count_rows('incidents') == 0
    assert await platform.count_rows('run_outcomes') == 0


@pytest.mark.asyncio
async def test_v2_ingress_fails_closed_without_database_source_policy(platform):
    await publish_v13(platform, with_policy=False)
    with pytest.raises(DomainError):
        await ingress(platform).accept(*signed(event()))
    assert await platform.count_rows('runs') == 0


@pytest.mark.asyncio
async def test_other_backend_policy_revocation_blocks_worker_and_deadline(platform):
    from workbench_agent.persistence.backends.sqlite import SQLiteBackend
    from workbench_agent.application.synthetic_source_policy import SyntheticSourcePolicy
    from workbench_agent.jobs.worker import Worker, Execution, DeploymentConnectorPolicy
    from workbench_agent.jobs.deadlines import DeadlineService
    from workbench_agent.runtime.graphs.registry import foundation_worker_registry
    from support.jobs import claim
    from sqlalchemy import update
    from workbench_agent.persistence import schema

    await publish_v13(platform)
    accepted = await ingress(platform).accept(*signed(event()))
    identities = {
        'v2-test': {
            'subject': platform.auth.subject,
            'team_id': platform.auth.team_id,
            'agent_ids': [platform.agent_id],
        }
    }
    policy = DeploymentConnectorPolicy(lambda: identities)
    owned = await claim(platform)
    await (
        foundation_worker_registry(platform.backend)
        ._executors['rca.synthetic']
        .execute(Execution(platform.backend, owned, policy))
    )
    import subprocess
    import sys

    script = """import asyncio,sys
from workbench_agent.persistence.backends.sqlite import SQLiteBackend
from workbench_agent.application.synthetic_source_policy import SyntheticSourcePolicy
async def run():
    backend=SQLiteBackend(sys.argv[1])
    try: assert await SyntheticSourcePolicy(backend).sync([],expected_revision=1)==2
    finally: await backend.close()
asyncio.run(run())"""
    result = subprocess.run(
        [sys.executable, '-c', script, platform.settings.storage.database_url.get_secret_value()],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    with pytest.raises(DomainError, match='SOURCE_POLICY_CONFLICT'):
        await SyntheticSourcePolicy(platform.backend).sync([], expected_revision=1)
    from workbench_agent.jobs.worker import CapabilityBlocked

    with pytest.raises(CapabilityBlocked):
        await Execution(platform.backend, owned, policy).check_operation()
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(
            update(schema.runs)
            .where(schema.runs.c.run_id == str(accepted.run_id))
            .values(deadline_at=0)
        )
    assert await DeadlineService(platform.backend, connector_policy=policy).expire_owned(
        owned.guard
    )
    result = await platform.get_run(accepted.run_id)
    assert result.status == 'timed_out' and result.outcome is None
    assert await platform.count_rows('incidents') == 0


@pytest.mark.asyncio
async def test_explicit_policy_sync_reads_revision_and_grants_from_settings(platform):
    from workbench_agent.cli.synthetic_source_policy import sync_source_policy, source_policy_status
    from workbench_agent.settings import SyntheticIngressBinding

    binding = SyntheticIngressBinding(
        protocol='synthetic-alert-v2',
        team_id=platform.auth.team_id,
        agent_id=platform.agent_id,
        actor_subject=platform.auth.subject,
        secret_ref='env://TEST_SECRET',
        identity_id='v2-test',
        allowed_scopes=[('checkout', 'test', 'id', 'error-rate')],
    )
    identities = {
        'v2-test': {
            'subject': platform.auth.subject,
            'team_id': platform.auth.team_id,
            'agent_ids': [platform.agent_id],
        }
    }
    settings = platform.settings.model_copy(
        update={
            'service': platform.settings.service.model_copy(
                update={
                    'synthetic_ingress': {'v2-test': binding},
                    'connector_identities': identities,
                }
            )
        }
    )
    assert (await source_policy_status(settings))['revision'] == 0
    assert (await sync_source_policy(settings, 0))['revision'] == 1
    with pytest.raises(DomainError, match='SOURCE_POLICY_CONFLICT'):
        await sync_source_policy(settings, 0)
    assert (await source_policy_status(settings))['revision'] == 1


@pytest.mark.asyncio
async def test_real_worker_blocks_after_source_allowlist_revoked(platform):
    from workbench_agent.application.synthetic_source_policy import SyntheticSourcePolicy
    from workbench_agent.jobs.worker import Worker, DeploymentConnectorPolicy
    from workbench_agent.runtime.graphs.registry import foundation_worker_registry

    await publish_v13(platform)
    await ingress(platform).accept(*signed(event()))
    await SyntheticSourcePolicy(platform.backend).sync([], expected_revision=1)
    policy = DeploymentConnectorPolicy(
        lambda: {
            'v2-test': {
                'subject': platform.auth.subject,
                'team_id': platform.auth.team_id,
                'agent_ids': [platform.agent_id],
            }
        }
    )
    assert (
        await Worker(
            platform.backend,
            'revoked-source',
            foundation_worker_registry(platform.backend),
            connector_policy=policy,
        ).run_once()
        == 'capability_blocked'
    )
    assert (
        await platform.count_rows('run_outcomes') == 0
        and await platform.count_rows('incidents') == 0
    )


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['worker', 'reconciler'])
async def test_identity_revoked_between_projection_check_and_commit_is_blocked(
    platform, monkeypatch, path
):
    from workbench_agent.jobs.worker import Worker, Execution, DeploymentConnectorPolicy
    from workbench_agent.jobs.reconciler import Reconciler
    from workbench_agent.runtime.graphs.registry import foundation_worker_registry
    from support.jobs import claim, expire_lease
    from workbench_agent.application.synthetic_source_policy import SyntheticSourcePolicy
    from workbench_agent.persistence.backends.sqlite import SQLiteBackend

    await publish_v13(platform)
    accepted = await ingress(platform).accept(*signed(event()))
    identities = {
        'v2-test': {
            'subject': platform.auth.subject,
            'team_id': platform.auth.team_id,
            'agent_ids': [platform.agent_id],
        },
        'another': {
            'subject': platform.auth.subject,
            'team_id': platform.auth.team_id,
            'agent_ids': [platform.agent_id],
        },
    }
    source_grant = dict(
        team_id=platform.auth.team_id,
        agent_id=platform.agent_id,
        connector_id='v2-test',
        identity_id='v2-test',
        subject=platform.auth.subject,
        service='checkout',
        environment='test',
        region='id',
        rule='error-rate',
    )
    # Preserve the grant while removing exactly the source identity from DB authority.
    await SyntheticSourcePolicy(platform.backend).sync(
        [source_grant], connector_identities=identities, expected_revision=1
    )
    other = SQLiteBackend(platform.settings.storage.database_url.get_secret_value())

    async def revoke():
        await SyntheticSourcePolicy(other).sync(
            [source_grant],
            connector_identities={'another': identities['another']},
            expected_revision=2,
        )

    policy = DeploymentConnectorPolicy(lambda: identities)
    registry = foundation_worker_registry(platform.backend)
    try:
        if path == 'worker':
            original = Execution.check_projection

            async def interleaved(self):
                await original(self)
                await revoke()

            monkeypatch.setattr(Execution, 'check_projection', interleaved)
            assert (
                await Worker(
                    platform.backend, 'racing-worker', registry, connector_policy=policy
                ).run_once()
                == 'recovery_scheduled'
            )
        else:
            owned = await claim(platform)
            await registry._executors['rca.synthetic'].execute(
                Execution(platform.backend, owned, policy)
            )
            await expire_lease(platform, owned.guard)
            reconciler = Reconciler(platform.backend, registry)
            reconciler.connector_policy = policy
            original = reconciler.prepare

            async def interleaved(claim):
                observation = await original(claim)
                await revoke()
                return observation

            monkeypatch.setattr(reconciler, 'prepare', interleaved)
            decision = await reconciler.recover(str(accepted.run_id))
            assert decision.action == 'unavailable'
        assert await platform.count_rows('incidents') == 0
        assert await platform.count_rows('run_outcomes') == 0
    finally:
        await other.close()


@pytest.mark.asyncio
async def test_deadline_blocks_when_db_identity_revoked_but_source_grant_remains(platform):
    from sqlalchemy import update
    from workbench_agent.persistence import schema
    from support.jobs import claim
    from workbench_agent.jobs.worker import Execution, DeploymentConnectorPolicy
    from workbench_agent.jobs.deadlines import DeadlineService
    from workbench_agent.runtime.graphs.registry import foundation_worker_registry
    from workbench_agent.application.synthetic_source_policy import SyntheticSourcePolicy

    await publish_v13(platform)
    accepted = await ingress(platform).accept(*signed(event()))
    identity = {
        'subject': platform.auth.subject,
        'team_id': platform.auth.team_id,
        'agent_ids': [platform.agent_id],
    }
    policy = DeploymentConnectorPolicy(lambda: {'v2-test': identity, 'another': identity})
    owned = await claim(platform)
    await (
        foundation_worker_registry(platform.backend)
        ._executors['rca.synthetic']
        .execute(Execution(platform.backend, owned, policy))
    )
    grant = dict(
        team_id=platform.auth.team_id,
        agent_id=platform.agent_id,
        connector_id='v2-test',
        identity_id='v2-test',
        subject=platform.auth.subject,
        service='checkout',
        environment='test',
        region='id',
        rule='error-rate',
    )
    await SyntheticSourcePolicy(platform.backend).sync(
        [grant], connector_identities={'another': identity}, expected_revision=1
    )
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(
            update(schema.runs)
            .where(schema.runs.c.run_id == str(accepted.run_id))
            .values(deadline_at=0)
        )
    assert await DeadlineService(platform.backend, connector_policy=policy).expire_owned(
        owned.guard
    )
    view = await platform.get_run(accepted.run_id)
    assert view.status == 'timed_out' and view.outcome is None
    assert (
        await platform.count_rows('incidents') == 0
        and await platform.count_rows('run_outcomes') == 0
    )


@pytest.mark.asyncio
async def test_legacy_source_policy_without_identity_directory_is_fail_closed(platform):
    from sqlalchemy import update
    from workbench_agent.persistence import schema
    from workbench_agent.application.idempotency import canonical, digest

    await publish_v13(platform)
    old = [
        dict(
            team_id=platform.auth.team_id,
            agent_id=platform.agent_id,
            connector_id='v2-test',
            identity_id='v2-test',
            subject=platform.auth.subject,
            service='checkout',
            environment='test',
            region='id',
            rule='error-rate',
        )
    ]
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(
            update(schema.synthetic_source_policy).values(
                policy_json=canonical(old), policy_hash=digest(old)
            )
        )
    with pytest.raises(DomainError):
        await ingress(platform).accept(*signed(event()))
    assert await platform.count_rows('runs') == 0

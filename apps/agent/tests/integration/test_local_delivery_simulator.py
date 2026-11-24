import pytest
from uuid import uuid4


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'kind,tool',
    [('jira', 'CreateTicket'), ('dod', 'CreateDutyEvent'), ('seatalk', 'SendNotification')],
)
async def test_local_simulator_is_durable_idempotent_and_scoped(tmp_path, kind, tool):
    from workbench_agent.delivery.local_simulator import LocalSimulator

    path = tmp_path / 'mock.sqlite3'
    action_id = str(uuid4())
    payload = {
        'target': {
            'server': 'local.mock.' + kind,
            'credential_scope': 'local-mock',
            'tool_name': tool,
        },
        'arguments': {'service': 'fictional'},
    }
    first = LocalSimulator(path, kind)
    receipt = await first.send(payload, action_id)
    again = LocalSimulator(path, kind)
    assert await again.send(payload, action_id) == receipt
    assert await again.lookup(payload, action_id) == receipt
    with pytest.raises(ValueError):
        await again.send({**payload, 'arguments': {'service': 'other'}}, action_id)
    with pytest.raises(ValueError):
        await again.send(
            {**payload, 'target': {**payload['target'], 'credential_scope': 'production'}},
            str(uuid4()),
        )


@pytest.mark.asyncio
async def test_local_simulator_lost_response_is_queryable_without_sending_twice(tmp_path):
    from workbench_agent.delivery.local_simulator import LocalSimulator, SimulatedLostResponse

    payload = {
        'target': {
            'server': 'local.mock.jira',
            'credential_scope': 'local-mock',
            'tool_name': 'CreateTicket',
        },
        'arguments': {'service': 'fictional'},
    }
    action_id = str(uuid4())
    service = LocalSimulator(tmp_path / 'mock.sqlite3', 'jira', lose_first_response=True)
    with pytest.raises(SimulatedLostResponse):
        await service.send(payload, action_id)
    assert await service.lookup(payload, action_id) == await LocalSimulator(
        tmp_path / 'mock.sqlite3', 'jira'
    ).send(payload, action_id)


def test_simulator_rejects_nonlocal_kind_and_relative_paths(tmp_path):
    from workbench_agent.delivery.local_simulator import LocalSimulator

    with pytest.raises(ValueError):
        LocalSimulator(tmp_path / 'mock.sqlite3', 'jira.example.com')
    with pytest.raises(ValueError):
        LocalSimulator('relative.sqlite3', 'jira')


@pytest.mark.asyncio
async def test_simulated_dispatch_lost_response_reconciles_without_redis(platform, tmp_path):
    from test_action_contract import setup_action
    from workbench_agent.delivery.local_simulator import LocalSimulator
    from workbench_agent.delivery.dispatcher import Dispatcher
    from workbench_agent.delivery.reconcile import ActionReconciler

    service, action = await setup_action(
        platform, server='local.mock.jira', credential_scope='local-mock'
    )
    await service.authorize(platform.auth, action['approval_id'], 'approve', action['payload_hash'])
    simulator = LocalSimulator(tmp_path / 'mock.sqlite3', 'jira', lose_first_response=True)
    platform.backend._delivery_senders = {'local.mock.jira': simulator}
    assert await Dispatcher(platform.backend, 'mock-dispatcher').dispatch_one() == 'unknown'
    assert await Dispatcher(platform.backend, 'mock-dispatcher').dispatch_one() is None
    result = await ActionReconciler(platform.backend).reconcile(platform.auth, action['action_id'])
    assert result['state'] == 'succeeded' and result['receipt']['external_id'].startswith(
        'sim-jira-'
    )


@pytest.mark.asyncio
async def test_explicit_local_dispatch_entrypoint_is_scoped_and_default_closed(platform, tmp_path):
    from workbench_agent.cli.local_dispatcher import dispatch_local_once
    from test_action_contract import setup_action

    service, action = await setup_action(
        platform, server='local.mock.jira', credential_scope='local-mock'
    )
    await service.authorize(platform.auth, action['approval_id'], 'approve', action['payload_hash'])
    with pytest.raises(ValueError):
        await dispatch_local_once(
            platform.settings, 'jira', platform.settings.storage.database_path
        )
    with pytest.raises(ValueError):
        await dispatch_local_once(
            platform.settings.model_copy(update={'environment': 'production'}),
            'jira',
            tmp_path / 'mock.sqlite3',
        )
    with pytest.raises(ValueError):
        await dispatch_local_once(platform.settings, 'real-jira', tmp_path / 'mock.sqlite3')
    report = await dispatch_local_once(platform.settings, 'jira', tmp_path / 'mock.sqlite3')
    assert report['status'] == 'succeeded'
    assert (await service.get(platform.auth, action['action_id']))['state'] == 'succeeded'


@pytest.mark.asyncio
async def test_simulated_temporary_failure_retries_and_revocation_stops_send(platform, tmp_path):
    from sqlalchemy import update
    from workbench_agent.persistence import schema
    from test_action_contract import setup_action, set_policy
    from workbench_agent.delivery.local_simulator import LocalSimulator
    from workbench_agent.delivery.dispatcher import Dispatcher

    service, action = await setup_action(
        platform, server='local.mock.jira', credential_scope='local-mock'
    )
    await service.authorize(platform.auth, action['approval_id'], 'approve', action['payload_hash'])
    simulator = LocalSimulator(tmp_path / 'mock.sqlite3', 'jira', fail_before_send=1)
    platform.backend._delivery_senders = {'local.mock.jira': simulator}
    assert await Dispatcher(platform.backend, 'local').dispatch_one() == 'retry'
    assert not (tmp_path / 'mock.sqlite3').exists()
    await set_policy(platform, rules={})
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(
            update(schema.outbox)
            .where(schema.outbox.c.action_id == action['action_id'])
            .values(available_at=0)
        )
    assert await Dispatcher(platform.backend, 'local').dispatch_one() == 'denied'
    assert not (tmp_path / 'mock.sqlite3').exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('delivery', ['normal', 'lost', 'rollback', 'stale', 'forged'])
async def test_verified_receipt_links_same_run_incident_atomically(
    platform, tmp_path, monkeypatch, delivery
):
    import json, time
    from datetime import datetime, timezone, timedelta
    from sqlalchemy import select, insert, update
    from uuid import uuid4
    from workbench_agent.application.idempotency import canonical, digest
    from workbench_agent.application.synthetic_source_policy import SyntheticSourcePolicy
    from test_action_contract import setup_action, set_policy
    from workbench_agent.delivery.local_simulator import LocalSimulator
    from workbench_agent.delivery.dispatcher import Dispatcher
    from workbench_agent.persistence import schema

    anchor = datetime.fromtimestamp(int(time.time()), timezone.utc)
    start = anchor - timedelta(seconds=600)
    scope_values = {
        'service': 'payment-api',
        'environment': 'test',
        'region': 'id',
        'start_at': start.isoformat(),
        'end_at': anchor.isoformat(),
    }
    service, action = await setup_action(
        platform, server='local.mock.jira', credential_scope='local-mock', scope_values=scope_values
    )
    await service.authorize(platform.auth, action['approval_id'], 'approve', action['payload_hash'])
    identities = {
        'mock-identity': {
            'subject': platform.auth.subject,
            'team_id': platform.auth.team_id,
            'agent_ids': [platform.agent_id],
        }
    }
    await set_policy(platform, connectors=identities)
    source_grant = dict(
        team_id=platform.auth.team_id,
        agent_id=platform.agent_id,
        connector_id='mock-source',
        identity_id='mock-identity',
        subject=platform.auth.subject,
        service='payment-api',
        environment='test',
        region='id',
        rule='error-rate',
    )
    await SyntheticSourcePolicy(platform.backend).sync(
        [source_grant], connector_identities=identities, expected_revision=0
    )
    async with platform.backend.create_uow().begin_write() as uow:
        row = (
            (
                await uow.execute(
                    select(schema.actions).where(schema.actions.c.action_id == action['action_id'])
                )
            )
            .mappings()
            .one()
        )
        run = await platform.backend.runs.one(
            uow, 'runs', team_id=platform.auth.team_id, run_id=row['run_id']
        )
        versions = json.loads(run['versions'])
        versions.update(
            graph_id='rca.synthetic', identity_kind='connector', agent_scope=[platform.agent_id]
        )
        await uow.execute(
            update(schema.runs)
            .where(schema.runs.c.run_id == run['run_id'])
            .values(versions=json.dumps(versions))
        )
        event = {
            'event_id': 'mock-alert',
            'text': 'fictional',
            'occurred_at': int(anchor.timestamp()),
            'region': 'id',
            'service': 'payment-api',
            'environment': 'test',
            'rule': 'error-rate',
            'anchor': anchor.isoformat(),
            'window_seconds': 600,
        }
        await uow.execute(
            insert(schema.synthetic_alert_sources).values(
                team_id=platform.auth.team_id,
                run_id=run['run_id'],
                connector_id='mock-source',
                identity_id='mock-identity',
                payload=canonical(event),
                payload_hash=digest(event),
                created_at=0,
            )
        )
        incident_id = str(uuid4())
        await uow.execute(
            insert(schema.incidents).values(
                team_id=platform.auth.team_id,
                incident_id=incident_id,
                run_id=run['run_id'],
                source='synthetic-alert-v2',
                service='payment-api',
                conclusion=canonical({'rule': 'error-rate', 'status': 'alert_observed'}),
                evidence=canonical({'scope_id': row['scope'], 'source_hash': digest(event)}),
                severity='unknown',
                created_at=0,
                updated_at=0,
            )
        )
        policy = await platform.backend.actions.policy(uow)
        await uow.execute(
            update(schema.approvals)
            .where(schema.approvals.c.approval_id == action['approval_id'])
            .values(
                policy_hash=digest(
                    {
                        'grant': platform.backend._action_grants[0],
                        'policy_revision': policy['revision'],
                    }
                )
            )
        )
    platform.backend._delivery_senders = {
        'local.mock.jira': LocalSimulator(
            tmp_path / 'mock.sqlite3', 'jira', lose_first_response=delivery == 'lost'
        )
    }
    if delivery == 'forged':
        fake = platform.backend._delivery_senders['local.mock.jira']

        async def wrong_receipt(payload, action_id):
            return {'external_id': 'sim-jira-not-the-action'}

        fake.send = wrong_receipt
        assert await Dispatcher(platform.backend, 'mock').dispatch_one() == 'succeeded'
        assert await platform.count_rows('incident_links') == 0
        return
    if delivery == 'stale':
        from workbench_agent.application.errors import DomainError

        fake = platform.backend._delivery_senders['local.mock.jira']
        original_send = fake.send

        async def concurrent_change(payload, action_id):
            receipt = await original_send(payload, action_id)
            async with platform.backend.create_uow().begin_write() as uow:
                await uow.execute(
                    update(schema.actions)
                    .where(schema.actions.c.action_id == action_id)
                    .values(state='failed')
                )
            return receipt

        fake.send = concurrent_change
        with pytest.raises(DomainError):
            await Dispatcher(platform.backend, 'mock').dispatch_one()
        assert await platform.count_rows('incident_links') == 0
        async with platform.backend.create_uow().begin_read() as uow:
            box = (
                (
                    await uow.execute(
                        select(schema.outbox).where(
                            schema.outbox.c.action_id == action['action_id']
                        )
                    )
                )
                .mappings()
                .one()
            )
        assert box['status'] == 'sending'
        return
    if delivery == 'rollback':
        import workbench_agent.incidents.delivery as projection

        original = projection.project_delivery_link

        async def crash(*args):
            await original(*args)
            raise RuntimeError('link transaction interrupted')

        monkeypatch.setattr(projection, 'project_delivery_link', crash)
        with pytest.raises(RuntimeError):
            await Dispatcher(platform.backend, 'mock').dispatch_one()
        assert await platform.count_rows('incident_action_links') == 0
        assert await platform.count_rows('incident_links') == 0
        return
    result = await Dispatcher(platform.backend, 'mock').dispatch_one()
    if delivery == 'lost':
        assert result == 'unknown'
        assert await platform.count_rows('incident_links') == 0
        from workbench_agent.delivery.reconcile import ActionReconciler

        assert (
            await ActionReconciler(platform.backend).reconcile(platform.auth, action['action_id'])
        )['state'] == 'succeeded'
    else:
        assert result == 'succeeded'
    async with platform.backend.create_uow().begin_read() as uow:
        records = (
            (
                await uow.execute(
                    select(schema.incident_action_links).where(
                        schema.incident_action_links.c.action_id == action['action_id']
                    )
                )
            )
            .mappings()
            .all()
        )
        assert len(records) == 1
        link = (
            (
                await uow.execute(
                    select(schema.incident_links).where(
                        schema.incident_links.c.link_id == records[0]['link_id']
                    )
                )
            )
            .mappings()
            .one()
        )
    assert (
        link['incident_id'] == incident_id
        and link['external_system'] == 'local.mock.jira'
        and link['external_id'].startswith('sim-jira-')
    )
    from workbench_agent.settings import SyntheticIngressBinding
    from workbench_agent.incidents.service import IncidentService

    binding = SyntheticIngressBinding(
        protocol='synthetic-alert-v2',
        team_id=platform.auth.team_id,
        agent_id=platform.agent_id,
        actor_subject=platform.auth.subject,
        identity_id='mock-identity',
        secret_ref='env://TEST',
        allowed_scopes=[('payment-api', 'test', 'id', 'error-rate')],
    )
    settings = platform.settings.model_copy(
        update={
            'service': platform.settings.service.model_copy(
                update={
                    'synthetic_ingress': {'mock-source': binding},
                    'connector_identities': identities,
                }
            )
        }
    )
    item = await IncidentService(platform.backend, settings).get(platform.auth, incident_id)
    assert item['delivery_links'] == [
        {
            'action_id': action['action_id'],
            'external_system': 'local.mock.jira',
            'external_id': link['external_id'],
        }
    ]
    await set_policy(platform, rules={})
    assert (await IncidentService(platform.backend, settings).get(platform.auth, incident_id))[
        'delivery_links'
    ] == []


@pytest.mark.asyncio
@pytest.mark.parametrize('graph_id', ['rca.proposal', 'rca.batch_proposal'])
async def test_pending_unfinished_v14_does_not_starve_another_team(platform, tmp_path, graph_id):
    import asyncio, json
    from dataclasses import replace
    from sqlalchemy import select, update, insert
    from uuid import uuid4
    from test_action_contract import setup_action
    from workbench_agent.delivery.local_simulator import LocalSimulator
    from workbench_agent.delivery.dispatcher import Dispatcher
    from workbench_agent.persistence import schema

    first_service, first = await setup_action(
        platform, server='local.mock.jira', credential_scope='local-mock'
    )
    await first_service.authorize(
        platform.auth, first['approval_id'], 'approve', first['payload_hash']
    )
    async with platform.backend.create_uow().begin_write() as uow:
        action = (
            (
                await uow.execute(
                    select(schema.actions).where(schema.actions.c.action_id == first['action_id'])
                )
            )
            .mappings()
            .one()
        )
        run = await platform.backend.runs.one(
            uow, 'runs', team_id=platform.auth.team_id, run_id=action['run_id']
        )
        agent = (
            await uow.execute(
                select(schema.agents.c.agent_id).where(
                    schema.agents.c.team_id == platform.other_auth.team_id
                )
            )
        ).scalar_one()
    other_settings = platform.settings.model_copy(
        update={
            'bootstrap': platform.settings.bootstrap.model_copy(
                update={
                    'team_id': platform.other_auth.team_id,
                    'agent_id': agent,
                    'admin_subject': platform.other_auth.subject,
                }
            )
        }
    )
    other = replace(platform, settings=other_settings, auth=platform.other_auth)
    second_service, second = await setup_action(
        other, server='local.mock.jira', credential_scope='local-mock'
    )
    await second_service.authorize(
        other.auth, second['approval_id'], 'approve', second['payload_hash']
    )
    async with platform.backend.create_uow().begin_write() as uow:
        version = json.loads(run['versions'])
        version['graph_id'] = graph_id
        await uow.execute(
            update(schema.runs)
            .where(schema.runs.c.run_id == run['run_id'])
            .values(versions=json.dumps(version))
        )
        original = (
            (
                await uow.execute(
                    select(schema.outbox).where(schema.outbox.c.action_id == first['action_id'])
                )
            )
            .mappings()
            .one()
        )
        # More than one batch of blocked rows must not starve the other tenant.
        await uow.execute(
            insert(schema.outbox),
            [{**dict(original), 'outbox_id': str(uuid4())} for _ in range(150)],
        )
    platform.backend._delivery_senders = {
        'local.mock.jira': LocalSimulator(tmp_path / 'mock.sqlite3', 'jira')
    }
    results = await asyncio.gather(
        Dispatcher(platform.backend, 'fair-a').dispatch_one(),
        Dispatcher(platform.backend, 'fair-b').dispatch_one(),
    )
    assert sorted(str(value) for value in results) == ['None', 'succeeded']
    assert (await second_service.get(other.auth, second['action_id']))['state'] == 'succeeded'
    async with platform.backend.create_uow().begin_read() as uow:
        rows = (
            (
                await uow.execute(
                    select(schema.outbox).where(schema.outbox.c.action_id == first['action_id'])
                )
            )
            .mappings()
            .all()
        )
    assert len(rows) == 151 and all(
        row['status'] == 'pending' and row['attempt'] == 0 for row in rows
    )


@pytest.mark.asyncio
@pytest.mark.parametrize('graph_id', ['rca.proposal', 'rca.batch_proposal'])
async def test_terminal_v14_without_incident_is_audited_and_dead(platform, tmp_path, graph_id):
    import asyncio, json
    from sqlalchemy import select, update
    from test_action_contract import setup_action
    from workbench_agent.delivery.local_simulator import LocalSimulator
    from workbench_agent.delivery.dispatcher import Dispatcher
    from workbench_agent.persistence import schema

    service, action = await setup_action(
        platform, server='local.mock.jira', credential_scope='local-mock'
    )
    await service.authorize(platform.auth, action['approval_id'], 'approve', action['payload_hash'])
    async with platform.backend.create_uow().begin_write() as uow:
        row = (
            (
                await uow.execute(
                    select(schema.actions).where(schema.actions.c.action_id == action['action_id'])
                )
            )
            .mappings()
            .one()
        )
        run = await platform.backend.runs.one(
            uow, 'runs', team_id=platform.auth.team_id, run_id=row['run_id']
        )
        versions = json.loads(run['versions'])
        versions['graph_id'] = graph_id
        await uow.execute(
            update(schema.runs)
            .where(schema.runs.c.run_id == run['run_id'])
            .values(versions=json.dumps(versions), status='partial')
        )
    platform.backend._delivery_senders = {
        'local.mock.jira': LocalSimulator(tmp_path / 'mock.sqlite3', 'jira')
    }
    results = await asyncio.gather(
        Dispatcher(platform.backend, 'fair-a').dispatch_one(),
        Dispatcher(platform.backend, 'fair-b').dispatch_one(),
    )
    assert sorted(str(value) for value in results) == ['None', 'denied']
    async with platform.backend.create_uow().begin_read() as uow:
        box = (
            (
                await uow.execute(
                    select(schema.outbox).where(schema.outbox.c.action_id == action['action_id'])
                )
            )
            .mappings()
            .one()
        )
        audit = (
            (
                await uow.execute(
                    select(schema.audit_logs).where(
                        schema.audit_logs.c.object == action['action_id'],
                        schema.audit_logs.c.operation == 'action.delivery.blocked',
                    )
                )
            )
            .mappings()
            .all()
        )
    assert box['status'] == 'dead' and box['attempt'] == 0 and len(audit) == 1
    assert not (tmp_path / 'mock.sqlite3').exists()

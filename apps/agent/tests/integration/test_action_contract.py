import pytest
from workbench_agent.application.errors import DomainError
from workbench_agent.domain.actions import action_payload_hash


def test_action_hash_is_canonical_and_binds_all_fields():
    first = action_payload_hash('tool.v1', {'project': 'test'}, {'a': 1, 'b': 2})
    assert first == action_payload_hash('tool.v1', {'project': 'test'}, {'b': 2, 'a': 1})
    assert first != action_payload_hash('tool.v2', {'project': 'test'}, {'a': 1, 'b': 2})
    assert first != action_payload_hash('tool.v1', {'project': 'other'}, {'a': 1, 'b': 2})


async def set_policy(platform, *, grants=None, rules=None, connectors=None):
    from workbench_agent.application.action_grants import ActionGrantService

    old = getattr(platform, '_test_action_policy', {'grants': [], 'rules': {}, 'connectors': {}})
    value = {
        'grants': old['grants'] if grants is None else grants,
        'rules': old['rules'] if rules is None else rules,
        'connectors': old['connectors'] if connectors is None else connectors,
    }
    async with platform.backend.create_uow().begin_read() as uow:
        current = await platform.backend.actions.policy(uow)
    await ActionGrantService(platform.backend).sync(
        value['grants'],
        value['rules'],
        value['connectors'],
        expected_revision=current['revision'] if current else 0,
    )
    platform._test_action_policy = value


async def setup_action(
    platform,
    *,
    server='ticket',
    credential_scope='write',
    tool_name='CreateTicket',
    scope_values=None,
):
    from workbench_agent.application.actions import ActionService
    from workbench_agent.domain.tools import ToolBinding
    from workbench_agent.domain.profiles import ProfileDraft
    from workbench_agent.tools.registry import ToolRegistry
    from support.skills import execution_for_run
    from workbench_agent.tools.permissions import DeploymentToolPermissions

    args = scope_values or {'service': 'payment-api'}
    binding = ToolBinding(
        alias='ticket.create',
        provider='mcp',
        tool_name=tool_name,
        server=server,
        credential_scope=credential_scope,
        permission='ticket.propose',
        read_only=False,
        input_schema={
            'type': 'object',
            'properties': {key: {'type': 'string'} for key in args},
            'required': list(args),
            'additionalProperties': False,
        },
    )
    ref = await ToolRegistry(platform.backend).publish(
        platform.auth, platform.agent_id, binding, key='publish-action'
    )
    platform.backend._action_grants = [
        {
            'team_id': platform.auth.team_id,
            'agent_id': platform.agent_id,
            'binding': ref.model_dump(mode='json'),
            'approver': platform.auth.subject,
            'arguments': args,
        }
    ]
    ctx = await platform.contexts.resolve(platform.auth, platform.agent_id)
    ws = await platform.profiles.get_agent(platform.auth, platform.agent_id)
    changed = await platform.profiles.save_draft(
        platform.auth,
        platform.agent_id,
        ProfileDraft(default_model=ctx.default_model, tools=(ref,)),
        ws['revision'],
        key='draft-action',
    )
    await platform.profiles.publish(
        platform.auth, platform.agent_id, changed['revision'], key='profile-action'
    )
    run = await platform.create_run()
    async with platform.backend.create_uow().begin_write() as uow:
        scope = await platform.contexts.query_scope(
            uow, platform.auth, run.run_id, scope_values or {'service': 'payment-api'}
        )
    rules = {
        'users': {platform.auth.subject: ['ticket.propose']},
        'teams': {platform.auth.team_id: ['ticket.propose']},
        'agents': {platform.agent_id: ['ticket.propose']},
        'skills': {'__none__': ['ticket.propose']},
        'experts': {'diagnosis': ['ticket.propose']},
    }
    platform.backend._tool_permissions = DeploymentToolPermissions(lambda: rules)
    await set_policy(platform, grants=platform.backend._action_grants, rules=rules, connectors={})
    execution = await execution_for_run(platform, run)
    platform._test_action_execution = execution
    service = ActionService(platform.backend)
    action = await service.propose(execution, 'test-action', ref, scope['scope_id'], args)
    return service, action


@pytest.mark.asyncio
async def test_proposal_approval_outbox_are_atomic_and_idempotent(platform):
    service, action = await setup_action(platform)
    assert action['state'] == 'proposed'
    with pytest.raises(DomainError):
        await service.authorize(platform.auth, action['approval_id'], 'approve', '0' * 64)
    result = await service.authorize(
        platform.auth, action['approval_id'], 'approve', action['payload_hash']
    )
    assert result['state'] == 'authorized'
    assert await platform.count_rows('actions') == 1
    assert await platform.count_rows('approvals') == 1
    assert await platform.count_rows('outbox') == 1


@pytest.mark.asyncio
async def test_unavailable_delivery_is_fail_closed(platform):
    from workbench_agent.delivery.dispatcher import Dispatcher

    dispatcher = Dispatcher(platform.backend, 'delivery-test')
    assert await dispatcher.dispatch_one() is None


@pytest.mark.asyncio
async def test_lost_response_becomes_unknown_and_never_resends(platform):
    from workbench_agent.delivery.dispatcher import Dispatcher

    service, action = await setup_action(platform)
    await service.authorize(platform.auth, action['approval_id'], 'approve', action['payload_hash'])

    class Lost:
        calls = 0

        async def send(self, payload, action_id):
            self.calls += 1
            raise TimeoutError()

    sender = Lost()
    platform.backend._delivery_senders = {'ticket': sender}
    dispatcher = Dispatcher(platform.backend, 'delivery-test')
    assert await dispatcher.dispatch_one() == 'unknown'
    assert await dispatcher.dispatch_one() is None
    assert sender.calls == 1
    row = await service.get(platform.auth, action['action_id'])
    assert row['state'] == 'unknown'


@pytest.mark.asyncio
async def test_unknown_action_reconciles_only_from_connector_fact(platform):
    from workbench_agent.delivery.dispatcher import Dispatcher
    from workbench_agent.delivery.reconcile import ActionReconciler

    service, action = await setup_action(platform)
    await service.authorize(platform.auth, action['approval_id'], 'approve', action['payload_hash'])

    class Connector:
        calls = 0

        async def send(self, payload, action_id):
            self.calls += 1
            raise TimeoutError()

        async def lookup(self, payload, action_id):
            return {'external_id': 'local-123'}

    connector = Connector()
    platform.backend._delivery_senders = {'ticket': connector}
    assert await Dispatcher(platform.backend, 'delivery-test').dispatch_one() == 'unknown'
    result = await ActionReconciler(platform.backend).reconcile(platform.auth, action['action_id'])
    assert result['state'] == 'succeeded'
    assert result['receipt']['external_id'] == 'local-123'
    assert connector.calls == 1


@pytest.mark.asyncio
async def test_revoked_tool_permission_never_dispatches(platform):
    from workbench_agent.delivery.dispatcher import Dispatcher
    from workbench_agent.tools.permissions import DeploymentToolPermissions

    service, action = await setup_action(platform)
    await service.authorize(platform.auth, action['approval_id'], 'approve', action['payload_hash'])
    platform.backend._tool_permissions = DeploymentToolPermissions(lambda: {})
    await set_policy(platform, rules={})

    class Sender:
        calls = 0

        async def send(self, *args):
            self.calls += 1

    sender = Sender()
    platform.backend._delivery_senders = {'ticket': sender}
    assert await Dispatcher(platform.backend, 'worker').dispatch_one() == 'denied'
    assert sender.calls == 0
    from sqlalchemy import select
    from workbench_agent.persistence import schema

    async with platform.backend.create_uow().begin_read() as uow:
        state = (
            await uow.execute(
                select(schema.actions.c.state).where(
                    schema.actions.c.action_id == action['action_id']
                )
            )
        ).scalar_one()
    assert state == 'failed'


@pytest.mark.asyncio
async def test_expired_sending_lease_is_unknown_without_retry(platform):
    from workbench_agent.delivery.dispatcher import Dispatcher
    from sqlalchemy import update
    from workbench_agent.persistence import schema

    service, action = await setup_action(platform)
    await service.authorize(platform.auth, action['approval_id'], 'approve', action['payload_hash'])
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(
            update(schema.outbox)
            .where(schema.outbox.c.action_id == action['action_id'])
            .values(status='sending', lease_owner='dead', lease_until=0, revision=1, attempt=1)
        )

    class Sender:
        calls = 0

        async def send(self, *args):
            self.calls += 1

    sender = Sender()
    platform.backend._delivery_senders = {'ticket': sender}
    assert await Dispatcher(platform.backend, 'other').dispatch_one() is None
    assert sender.calls == 0
    assert (await service.get(platform.auth, action['action_id']))['state'] == 'unknown'


@pytest.mark.asyncio
async def test_rejected_approval_never_queues_delivery(platform):
    service, action = await setup_action(platform)
    result = await service.authorize(
        platform.auth, action['approval_id'], 'reject', action['payload_hash']
    )
    assert result['state'] == 'rejected'
    assert await platform.count_rows('outbox') == 0


@pytest.mark.asyncio
async def test_approval_replay_is_idempotent_and_conflicting_decision_rejected(platform):
    service, action = await setup_action(platform)
    first = await service.authorize(
        platform.auth, action['approval_id'], 'approve', action['payload_hash']
    )
    again = await service.authorize(
        platform.auth, action['approval_id'], 'approve', action['payload_hash']
    )
    assert again == first
    assert await platform.count_rows('outbox') == 1
    with pytest.raises(DomainError):
        await service.authorize(
            platform.auth, action['approval_id'], 'reject', action['payload_hash']
        )


@pytest.mark.asyncio
async def test_hung_sender_is_bounded_and_unknown(platform):
    import asyncio
    from workbench_agent.delivery.dispatcher import Dispatcher

    service, action = await setup_action(platform)
    await service.authorize(platform.auth, action['approval_id'], 'approve', action['payload_hash'])

    class Hung:
        async def send(self, *args):
            await asyncio.sleep(10)

    platform.backend._delivery_senders = {'ticket': Hung()}
    result = await asyncio.wait_for(
        Dispatcher(platform.backend, 'worker', lease_ms=1000).dispatch_one(), timeout=3
    )
    assert result == 'unknown'
    assert (await service.get(platform.auth, action['action_id']))['state'] == 'unknown'


@pytest.mark.asyncio
async def test_competing_dispatchers_send_only_once(platform):
    import asyncio
    from workbench_agent.delivery.dispatcher import Dispatcher

    service, action = await setup_action(platform)
    await service.authorize(platform.auth, action['approval_id'], 'approve', action['payload_hash'])

    class Sender:
        calls = 0

        async def send(self, *args):
            self.calls += 1
            await asyncio.sleep(0.05)
            return {'external_id': 'ticket-local'}

    sender = Sender()
    platform.backend._delivery_senders = {'ticket': sender}
    values = await asyncio.gather(
        Dispatcher(platform.backend, 'one').dispatch_one(),
        Dispatcher(platform.backend, 'two').dispatch_one(),
    )
    assert values.count('succeeded') == 1
    assert sender.calls == 1
    assert (await service.get(platform.auth, action['action_id']))['state'] == 'succeeded'


@pytest.mark.asyncio
async def test_scope_change_blocks_approval(platform):
    service, action = await setup_action(platform)
    from sqlalchemy import select
    from workbench_agent.persistence import schema

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
        await platform.contexts.query_scope(uow, platform.auth, row['run_id'], {'service': 'other'})
    with pytest.raises(DomainError) as error:
        await service.authorize(
            platform.auth, action['approval_id'], 'approve', action['payload_hash']
        )
    assert error.value.code == 'STALE_QUERY_SCOPE'
    assert await platform.count_rows('outbox') == 0


@pytest.mark.asyncio
async def test_action_and_delivery_changes_are_audited(platform):
    from workbench_agent.delivery.dispatcher import Dispatcher
    from sqlalchemy import select
    from workbench_agent.persistence import schema

    service, action = await setup_action(platform)
    await service.authorize(platform.auth, action['approval_id'], 'approve', action['payload_hash'])

    class Sender:
        async def send(self, *args):
            return {'external_id': 'local-123'}

    platform.backend._delivery_senders = {'ticket': Sender()}
    assert await Dispatcher(platform.backend, 'delivery-test').dispatch_one() == 'succeeded'
    async with platform.backend.create_uow().begin_read() as uow:
        operations = set((await uow.execute(select(schema.audit_logs.c.operation))).scalars())
        events = set((await uow.execute(select(schema.run_events.c.type))).scalars())
    assert {'action.propose', 'action.authorize', 'action.delivery'} <= operations
    assert {'action.updated', 'delivery.updated'} <= events


@pytest.mark.asyncio
async def test_untrusted_receipt_extra_fields_are_not_persisted(platform):
    from workbench_agent.delivery.dispatcher import Dispatcher
    from sqlalchemy import select
    from workbench_agent.persistence import schema

    service, action = await setup_action(platform)
    await service.authorize(platform.auth, action['approval_id'], 'approve', action['payload_hash'])

    class Sender:
        async def send(self, *args):
            return {'external_id': 'local-123', 'raw_response': 'sensitive'}

    platform.backend._delivery_senders = {'ticket': Sender()}
    assert await Dispatcher(platform.backend, 'delivery-test').dispatch_one() == 'unknown'
    async with platform.backend.create_uow().begin_read() as uow:
        receipt = (
            await uow.execute(
                select(schema.actions.c.receipt).where(
                    schema.actions.c.action_id == action['action_id']
                )
            )
        ).scalar_one()
    assert receipt is None


@pytest.mark.asyncio
async def test_action_approval_http_is_scoped_and_idempotent(platform, client):
    service, action = await setup_action(platform)
    path = f'/v1/actions/{action["action_id"]}'
    response = await client.get(path)
    assert response.status_code == 200 and response.json()['state'] == 'proposed'
    details = response.json()
    assert details['approval_id'] == action['approval_id']
    assert details['payload_hash'] == action['payload_hash']
    assert details['arguments'] == {'service': 'payment-api'}
    assert details['target'] == {
        'server': 'ticket',
        'credential_scope': 'write',
        'tool_name': 'CreateTicket',
    }
    assert details['scope'] == {'service': 'payment-api'}
    assert details['binding']['version'] and details['expires_at'] > 0
    body = {'decision': 'approve', 'expected_args_hash': action['payload_hash']}
    response = await client.post(f'/v1/approvals/{action["approval_id"]}/decision', json=body)
    assert response.status_code == 200 and response.json()['state'] == 'authorized'
    assert (
        await client.post(f'/v1/approvals/{action["approval_id"]}/decision', json=body)
    ).json() == response.json()
    assert await platform.count_rows('outbox') == 1


@pytest.mark.asyncio
async def test_review_projection_matches_live_action_approval_eligibility(platform):
    from workbench_agent.application.review_facts import action_review_facts
    from workbench_agent.application.review_eligibility import review_eligibility

    service, action = await setup_action(platform)
    async with platform.backend.create_uow().begin_read() as uow:
        action_row = await platform.backend.actions.one(
            uow, 'actions', team_id=platform.auth.team_id, action_id=action['action_id']
        )
        outbox = await platform.backend.actions.one(
            uow, 'outbox', team_id=platform.auth.team_id, action_id=action['action_id']
        )
        facts = await action_review_facts(platform.backend, uow, platform.auth, action_row, outbox)
    result = review_eligibility('action_approval', facts=facts, now_ms=facts.get('now_ms', 0))
    assert result.visible is True
    assert result.operations == ('approve', 'reject')


@pytest.mark.asyncio
async def test_review_projection_limits_unknown_delivery_to_reconcile(platform):
    from workbench_agent.application.review_facts import action_review_facts
    from workbench_agent.application.review_eligibility import review_eligibility
    from workbench_agent.delivery.dispatcher import Dispatcher

    service, action = await setup_action(platform)
    await service.authorize(platform.auth, action['approval_id'], 'approve', action['payload_hash'])

    class Connector:
        async def send(self, *args):
            raise TimeoutError()

        async def lookup(self, *args):
            return None

    platform.backend._delivery_senders = {'ticket': Connector()}
    assert await Dispatcher(platform.backend, 'delivery-test').dispatch_one() == 'unknown'
    async with platform.backend.create_uow().begin_read() as uow:
        action_row = await platform.backend.actions.one(
            uow, 'actions', team_id=platform.auth.team_id, action_id=action['action_id']
        )
        outbox = await platform.backend.actions.one(
            uow, 'outbox', team_id=platform.auth.team_id, action_id=action['action_id']
        )
        facts = await action_review_facts(platform.backend, uow, platform.auth, action_row, outbox)
    result = review_eligibility('action_reconcile', facts=facts, now_ms=facts.get('now_ms', 0))
    assert result.visible is True
    assert result.operations == ('reconcile',)


@pytest.mark.asyncio
async def test_reconcile_scope_change_does_not_send_payload(platform):
    from workbench_agent.delivery.dispatcher import Dispatcher
    from workbench_agent.delivery.reconcile import ActionReconciler
    from sqlalchemy import select
    from workbench_agent.persistence import schema

    service, action = await setup_action(platform)
    await service.authorize(platform.auth, action['approval_id'], 'approve', action['payload_hash'])

    class Connector:
        calls = 0

        async def send(self, *args):
            raise TimeoutError()

        async def lookup(self, *args):
            self.calls += 1
            return {'external_id': 'local-123'}

    connector = Connector()
    platform.backend._delivery_senders = {'ticket': connector}
    assert await Dispatcher(platform.backend, 'worker').dispatch_one() == 'unknown'
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
        await platform.contexts.query_scope(uow, platform.auth, row['run_id'], {'service': 'other'})
    with pytest.raises(DomainError):
        await ActionReconciler(platform.backend).reconcile(platform.auth, action['action_id'])
    assert connector.calls == 0


@pytest.mark.asyncio
async def test_reconcile_revoked_tool_permission_does_not_send_payload(platform):
    from workbench_agent.delivery.dispatcher import Dispatcher
    from workbench_agent.delivery.reconcile import ActionReconciler
    from workbench_agent.tools.permissions import DeploymentToolPermissions

    service, action = await setup_action(platform)
    await service.authorize(platform.auth, action['approval_id'], 'approve', action['payload_hash'])

    class Connector:
        calls = 0

        async def send(self, *args):
            raise TimeoutError()

        async def lookup(self, *args):
            self.calls += 1
            return {'external_id': 'local-123'}

    connector = Connector()
    platform.backend._delivery_senders = {'ticket': connector}
    assert await Dispatcher(platform.backend, 'worker').dispatch_one() == 'unknown'
    platform.backend._tool_permissions = DeploymentToolPermissions(lambda: {})
    await set_policy(platform, rules={})
    with pytest.raises(DomainError):
        await ActionReconciler(platform.backend).reconcile(platform.auth, action['action_id'])
    assert connector.calls == 0


@pytest.mark.asyncio
async def test_reconcile_revoked_during_lookup_does_not_write_receipt(platform):
    from workbench_agent.delivery.dispatcher import Dispatcher
    from workbench_agent.delivery.reconcile import ActionReconciler
    from workbench_agent.tools.permissions import DeploymentToolPermissions

    service, action = await setup_action(platform)
    await service.authorize(platform.auth, action['approval_id'], 'approve', action['payload_hash'])

    class Connector:
        async def send(self, *args):
            raise TimeoutError()

        async def lookup(self, *args):
            platform.backend._tool_permissions = DeploymentToolPermissions(lambda: {})
            await set_policy(platform, rules={})
            return {'external_id': 'local-123'}

    platform.backend._delivery_senders = {'ticket': Connector()}
    assert await Dispatcher(platform.backend, 'worker').dispatch_one() == 'unknown'
    with pytest.raises(DomainError):
        await ActionReconciler(platform.backend).reconcile(platform.auth, action['action_id'])
    from sqlalchemy import select
    from workbench_agent.persistence import schema

    async with platform.backend.create_uow().begin_read() as uow:
        state = (
            await uow.execute(
                select(schema.actions.c.state).where(
                    schema.actions.c.action_id == action['action_id']
                )
            )
        ).scalar_one()
    assert state == 'unknown'


@pytest.mark.asyncio
async def test_connector_run_can_only_be_approved_by_deployment_named_human(platform):
    from sqlalchemy import update
    from workbench_agent.persistence import schema
    from workbench_agent.jobs.worker import DeploymentConnectorPolicy
    from workbench_agent.domain.identity import AuthContext
    from workbench_agent.domain.tools import ToolBinding
    from workbench_agent.domain.profiles import ProfileDraft
    from workbench_agent.tools.registry import ToolRegistry
    from workbench_agent.tools.permissions import DeploymentToolPermissions
    from support.skills import execution_for_run
    from workbench_agent.application.actions import ActionService

    binding = ToolBinding(
        alias='ticket.create',
        provider='mcp',
        tool_name='CreateTicket',
        server='ticket',
        credential_scope='write',
        permission='ticket.propose',
        read_only=False,
        input_schema={
            'type': 'object',
            'properties': {'service': {'type': 'string'}},
            'required': ['service'],
            'additionalProperties': False,
        },
    )
    ref = await ToolRegistry(platform.backend).publish(
        platform.auth, platform.agent_id, binding, key='connector-action'
    )
    ctx = await platform.contexts.resolve(platform.auth, platform.agent_id)
    ws = await platform.profiles.get_agent(platform.auth, platform.agent_id)
    changed = await platform.profiles.save_draft(
        platform.auth,
        platform.agent_id,
        ProfileDraft(default_model=ctx.default_model, tools=(ref,)),
        ws['revision'],
        key='connector-draft',
    )
    await platform.profiles.publish(
        platform.auth, platform.agent_id, changed['revision'], key='connector-publish'
    )
    async with platform.backend.create_uow().begin_read() as uow:
        team = await platform.backend.configuration.one(uow, 'teams', team_id=platform.auth.team_id)
    await platform.teams.set_member(
        platform.auth, 'service-bot', 'operator', 'active', team['revision'], key='bot-member'
    )
    run = await platform.create_run()
    async with platform.backend.create_uow().begin_write() as uow:
        row = await platform.backend.runs.one(
            uow, 'runs', team_id=platform.auth.team_id, run_id=run.run_id
        )
        import json

        versions = json.loads(row['versions'])
        versions.update(subject='service-bot', identity_kind='connector', roles=['operator'])
        await uow.execute(
            update(schema.runs)
            .where(schema.runs.c.run_id == run.run_id)
            .values(versions=json.dumps(versions))
        )
        scope = await platform.contexts.query_scope(
            uow, platform.auth, run.run_id, {'service': 'payment-api'}
        )
    rules = {
        'users': {'service-bot': ['ticket.propose'], platform.auth.subject: ['ticket.propose']},
        'teams': {platform.auth.team_id: ['ticket.propose']},
        'agents': {platform.agent_id: ['ticket.propose']},
        'skills': {'__none__': ['ticket.propose']},
        'experts': {'diagnosis': ['ticket.propose']},
    }
    platform.backend._tool_permissions = DeploymentToolPermissions(lambda: rules)
    connector_policy = DeploymentConnectorPolicy(
        lambda: {
            'local': {
                'subject': 'service-bot',
                'team_id': platform.auth.team_id,
                'agent_ids': [platform.agent_id],
            }
        }
    )
    platform.backend._delivery_connector_policy = connector_policy
    platform.backend._action_grants = [
        {
            'team_id': platform.auth.team_id,
            'agent_id': platform.agent_id,
            'binding': ref.model_dump(mode='json'),
            'approver': platform.auth.subject,
            'arguments': {'service': 'payment-api'},
        }
    ]
    await set_policy(
        platform,
        grants=platform.backend._action_grants,
        rules=rules,
        connectors={
            'local': {
                'subject': 'service-bot',
                'team_id': platform.auth.team_id,
                'agent_ids': [platform.agent_id],
            }
        },
    )
    execution = await execution_for_run(platform, run)
    execution.connector_policy = connector_policy
    service = ActionService(platform.backend)
    with pytest.raises(DomainError):
        await service.propose(
            execution, 'wrong-target', ref, scope['scope_id'], {'service': 'different-service'}
        )
    action = await service.propose(
        execution, 'connector-action', ref, scope['scope_id'], {'service': 'payment-api'}
    )
    with pytest.raises(DomainError):
        await service.authorize(
            AuthContext(
                team_id=platform.auth.team_id,
                subject='service-bot',
                roles=('operator',),
                identity_kind='connector',
            ),
            action['approval_id'],
            'approve',
            action['payload_hash'],
        )
    assert (
        await service.authorize(
            platform.auth, action['approval_id'], 'approve', action['payload_hash']
        )
    )['state'] == 'authorized'
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(
            update(schema.team_members)
            .where(
                schema.team_members.c.team_id == platform.auth.team_id,
                schema.team_members.c.subject == platform.auth.subject,
            )
            .values(status='revoked')
        )

    class Sender:
        calls = 0

        async def send(self, *args):
            self.calls += 1

    sender = Sender()
    platform.backend._delivery_senders = {'ticket': sender}
    from workbench_agent.delivery.dispatcher import Dispatcher

    assert await Dispatcher(platform.backend, 'worker').dispatch_one() == 'denied'
    assert sender.calls == 0


@pytest.mark.asyncio
async def test_revoked_approver_stops_dispatch(platform):
    from workbench_agent.delivery.dispatcher import Dispatcher
    from sqlalchemy import update
    from workbench_agent.persistence import schema

    service, action = await setup_action(platform)
    await service.authorize(platform.auth, action['approval_id'], 'approve', action['payload_hash'])
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(
            update(schema.team_members)
            .where(
                schema.team_members.c.team_id == platform.auth.team_id,
                schema.team_members.c.subject == platform.auth.subject,
            )
            .values(status='revoked')
        )

    class Sender:
        calls = 0

        async def send(self, *args):
            self.calls += 1

    sender = Sender()
    platform.backend._delivery_senders = {'ticket': sender}
    assert await Dispatcher(platform.backend, 'delivery-test').dispatch_one() == 'denied'
    assert sender.calls == 0


@pytest.mark.asyncio
async def test_revoked_target_grant_stops_dispatch(platform):
    from workbench_agent.delivery.dispatcher import Dispatcher

    service, action = await setup_action(platform)
    await service.authorize(platform.auth, action['approval_id'], 'approve', action['payload_hash'])
    platform.backend._action_grants = []
    await set_policy(platform, grants=[])

    class Sender:
        calls = 0

        async def send(self, *args):
            self.calls += 1

    sender = Sender()
    platform.backend._delivery_senders = {'ticket': sender}
    assert await Dispatcher(platform.backend, 'delivery-test').dispatch_one() == 'denied'
    assert sender.calls == 0


@pytest.mark.asyncio
async def test_approval_by_different_active_human_is_denied(platform):
    from workbench_agent.domain.identity import AuthContext

    service, action = await setup_action(platform)
    async with platform.backend.create_uow().begin_read() as uow:
        team = await platform.backend.configuration.one(uow, 'teams', team_id=platform.auth.team_id)
    await platform.teams.set_member(
        platform.auth, 'other-person', 'operator', 'active', team['revision'], key='add-person'
    )
    other = AuthContext(team_id=platform.auth.team_id, subject='other-person', roles=('operator',))
    with pytest.raises(DomainError):
        await service.authorize(other, action['approval_id'], 'approve', action['payload_hash'])
    with pytest.raises(DomainError):
        await service.get(other, action['action_id'])
    assert await platform.count_rows('outbox') == 0


@pytest.mark.asyncio
async def test_changed_exact_target_grant_cannot_authorize_proposal(platform):
    service, action = await setup_action(platform)
    platform.backend._action_grants[0]['arguments'] = {'service': 'other-service'}
    await set_policy(platform, grants=platform.backend._action_grants)
    with pytest.raises(DomainError):
        await service.authorize(
            platform.auth, action['approval_id'], 'approve', action['payload_hash']
        )
    assert await platform.count_rows('outbox') == 0


@pytest.mark.asyncio
async def test_proven_not_sent_retries_with_limit_while_unknown_never_retries(platform):
    from workbench_agent.delivery.dispatcher import Dispatcher, DeliveryNotSent
    from sqlalchemy import update, select
    from workbench_agent.persistence import schema

    service, action = await setup_action(platform)
    await service.authorize(platform.auth, action['approval_id'], 'approve', action['payload_hash'])

    class Sender:
        calls = 0

        async def send(self, *args):
            self.calls += 1
            raise DeliveryNotSent()

    sender = Sender()
    platform.backend._delivery_senders = {'ticket': sender}
    dispatcher = Dispatcher(platform.backend, 'worker')
    for attempt in range(1, 9):
        assert await dispatcher.dispatch_one() == ('dead' if attempt == 8 else 'retry')
        async with platform.backend.create_uow().begin_write() as uow:
            row = (
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
            assert row['attempt'] == attempt
            if attempt < 8:
                await uow.execute(
                    update(schema.outbox)
                    .where(schema.outbox.c.outbox_id == row['outbox_id'])
                    .values(available_at=0)
                )
    assert sender.calls == 8
    assert await dispatcher.dispatch_one() is None
    from sqlalchemy import select
    from workbench_agent.persistence import schema

    async with platform.backend.create_uow().begin_read() as uow:
        state = (
            await uow.execute(
                select(schema.actions.c.state).where(
                    schema.actions.c.action_id == action['action_id']
                )
            )
        ).scalar_one()
    assert state == 'failed'


@pytest.mark.asyncio
async def test_action_review_details_hide_after_scope_change(platform, client):
    from sqlalchemy import select
    from workbench_agent.persistence import schema

    service, action = await setup_action(platform)
    assert (await client.get(f'/v1/actions/{action["action_id"]}')).status_code == 200
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
        await platform.contexts.query_scope(uow, platform.auth, row['run_id'], {'service': 'other'})
    assert (await client.get(f'/v1/actions/{action["action_id"]}')).status_code in (403, 404, 409)


@pytest.mark.asyncio
async def test_action_review_http_rejects_wrong_user_and_revoked_permission(platform, client):
    from workbench_agent.tools.permissions import DeploymentToolPermissions

    service, action = await setup_action(platform)
    async with platform.backend.create_uow().begin_read() as uow:
        team = await platform.backend.configuration.one(uow, 'teams', team_id=platform.auth.team_id)
    await platform.teams.set_member(
        platform.auth, 'other-person', 'operator', 'active', team['revision'], key='add-other'
    )
    path = f'/v1/actions/{action["action_id"]}'
    wrong = {'Authorization': f'Bearer development:{platform.auth.team_id}:other-person'}
    assert (await client.get(path, headers=wrong)).status_code in (403, 404)
    assert (await client.get(path)).status_code == 200
    platform.backend._tool_permissions = DeploymentToolPermissions(lambda: {})
    await set_policy(platform, rules={})
    assert (await client.get(path)).status_code in (403, 404)


def test_deployment_action_grant_is_validated_and_default_closed(config_data):
    from workbench_agent.settings import Settings
    from pydantic import ValidationError

    assert Settings.model_validate(config_data).service.action_grants == ()
    from uuid import uuid4

    grant = {
        'team_id': str(uuid4()),
        'agent_id': str(uuid4()),
        'binding': {'resource_id': str(uuid4()), 'version': '1', 'content_hash': 'a' * 64},
        'approver': 'human',
        'arguments': {'service': 'payment-api'},
    }
    configured = {**config_data, 'service': {'action_grants': [grant]}}
    assert Settings.model_validate(configured).service.action_grants[0].approver == 'human'
    for invalid in (
        {**grant, 'approver': ''},
        {**grant, 'binding': {**grant['binding'], 'content_hash': 'bad'}},
        {**grant, 'arguments': {}},
        {**grant, 'team_id': 'wrong'},
    ):
        with pytest.raises(ValidationError):
            Settings.model_validate({**config_data, 'service': {'action_grants': [invalid]}})


@pytest.mark.asyncio
async def test_api_composition_uses_only_explicit_deployment_action_grants(platform):
    from workbench_agent.settings import ActionDeploymentGrant
    from workbench_agent.api.app import create_app
    from uuid import uuid4

    grant = ActionDeploymentGrant.model_validate(
        {
            'team_id': platform.auth.team_id,
            'agent_id': platform.agent_id,
            'binding': {'resource_id': str(uuid4()), 'version': '1', 'content_hash': 'a' * 64},
            'approver': platform.auth.subject,
            'arguments': {'service': 'payment-api'},
        }
    )
    settings = platform.settings.model_copy(
        update={'service': platform.settings.service.model_copy(update={'action_grants': (grant,)})}
    )
    app = create_app(settings)
    async with app.state.backend.create_uow().begin_read() as uow:
        assert await app.state.backend.actions.policy(uow) is None
    assert not getattr(app.state.backend, '_delivery_senders', None)
    await app.state.backend.close()


@pytest.mark.asyncio
async def test_database_grant_revocation_is_immediate_across_independent_backends(platform):
    from workbench_agent.application.action_grants import ActionGrantService
    from workbench_agent.persistence.backends.sqlite import SQLiteBackend
    from workbench_agent.tools.permissions import DeploymentToolPermissions
    from workbench_agent.delivery.dispatcher import Dispatcher

    service, action = await setup_action(platform)
    grants = list(platform.backend._action_grants)
    await ActionGrantService(platform.backend).sync(
        grants, platform._test_action_policy['rules'], {}, expected_revision=1
    )
    await service.authorize(platform.auth, action['approval_id'], 'approve', action['payload_hash'])
    worker = SQLiteBackend(
        platform.settings.storage.database_url.get_secret_value(),
        tool_permissions=platform.backend._tool_permissions,
    )
    worker._action_grants = grants  # A stale process-local snapshot must not grant authority.

    class Sender:
        calls = 0

        async def send(self, *args):
            self.calls += 1

    sender = Sender()
    worker._delivery_senders = {'ticket': sender}
    try:
        await ActionGrantService(platform.backend).sync(
            [], platform._test_action_policy['rules'], {}, expected_revision=1
        )
        assert await Dispatcher(worker, 'worker').dispatch_one() == 'denied'
        assert sender.calls == 0
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_explicit_action_policy_maintenance_sync_is_shared(platform):
    from workbench_agent.cli.action_policy import sync_action_policy
    from workbench_agent.application.action_grants import ActionGrantService
    from workbench_agent.persistence.backends.sqlite import SQLiteBackend
    from workbench_agent.settings import ActionDeploymentGrant
    from uuid import uuid4

    grant = ActionDeploymentGrant.model_validate(
        {
            'team_id': platform.auth.team_id,
            'agent_id': platform.agent_id,
            'binding': {'resource_id': str(uuid4()), 'version': '1', 'content_hash': 'a' * 64},
            'approver': platform.auth.subject,
            'arguments': {'service': 'payment-api'},
        }
    )
    service = platform.settings.service.model_copy(update={'action_grants': (grant,)})
    settings = platform.settings.model_copy(update={'service': service})
    assert (await sync_action_policy(settings, 0))['revision'] == 1
    worker = SQLiteBackend(settings.storage.database_url.get_secret_value())
    try:
        async with worker.create_uow().begin_read() as uow:
            policy = await worker.actions.policy(uow)
        import json

        assert json.loads(policy['policy_json'])['grants'] == [grant.model_dump(mode='json')]
        assert await ActionGrantService(worker).sync([], {}, {}, expected_revision=1) == 2
        async with platform.backend.create_uow().begin_read() as uow:
            assert (await platform.backend.actions.policy(uow))['revision'] == 2
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_revocation_then_regrant_never_revives_old_approval(platform):
    from workbench_agent.application.action_grants import ActionGrantService
    from workbench_agent.delivery.dispatcher import Dispatcher

    service, action = await setup_action(platform)
    await service.authorize(platform.auth, action['approval_id'], 'approve', action['payload_hash'])
    policy = platform._test_action_policy
    control = ActionGrantService(platform.backend)
    await control.sync([], policy['rules'], policy['connectors'], expected_revision=1)
    await control.sync(policy['grants'], policy['rules'], policy['connectors'], expected_revision=2)

    class Sender:
        calls = 0

        async def send(self, *args):
            self.calls += 1

    sender = Sender()
    platform.backend._delivery_senders = {'ticket': sender}
    assert await Dispatcher(platform.backend, 'worker').dispatch_one() == 'denied'
    assert sender.calls == 0


@pytest.mark.asyncio
async def test_separate_backend_policy_revocation_during_lookup_prevents_receipt(platform):
    from workbench_agent.application.action_grants import ActionGrantService
    from workbench_agent.delivery.dispatcher import Dispatcher
    from workbench_agent.delivery.reconcile import ActionReconciler
    from workbench_agent.persistence.backends.sqlite import SQLiteBackend
    from sqlalchemy import select
    from workbench_agent.persistence import schema

    service, action = await setup_action(platform)
    await service.authorize(platform.auth, action['approval_id'], 'approve', action['payload_hash'])
    admin = SQLiteBackend(platform.settings.storage.database_url.get_secret_value())
    policy = platform._test_action_policy

    class Connector:
        async def send(self, *args):
            raise TimeoutError()

        async def lookup(self, *args):
            await ActionGrantService(admin).sync(
                [], policy['rules'], policy['connectors'], expected_revision=1
            )
            return {'external_id': 'local-123'}

    platform.backend._delivery_senders = {'ticket': Connector()}
    try:
        assert await Dispatcher(platform.backend, 'worker').dispatch_one() == 'unknown'
        with pytest.raises(DomainError):
            await ActionReconciler(platform.backend).reconcile(platform.auth, action['action_id'])
        async with platform.backend.create_uow().begin_read() as uow:
            state = (
                await uow.execute(
                    select(schema.actions.c.state).where(
                        schema.actions.c.action_id == action['action_id']
                    )
                )
            ).scalar_one()
        assert state == 'unknown'
    finally:
        await admin.close()


@pytest.mark.asyncio
async def test_proposal_rechecks_running_status_in_write_transaction(platform, monkeypatch):
    from sqlalchemy import select, update
    from workbench_agent.persistence import schema
    from workbench_agent.persistence.errors import StaleExecution
    from workbench_agent.domain.profiles import VersionRef
    import json

    service, action = await setup_action(platform)
    execution = platform._test_action_execution
    async with platform.backend.create_uow().begin_read() as uow:
        row = (
            (
                await uow.execute(
                    select(schema.actions).where(schema.actions.c.action_id == action['action_id'])
                )
            )
            .mappings()
            .one()
        )
    ref = VersionRef.model_validate(json.loads(row['payload_ref'])['binding'])
    original = execution.check_operation

    async def races():
        await original()
        async with platform.backend.create_uow().begin_write() as uow:
            await uow.execute(
                update(schema.runs)
                .where(schema.runs.c.run_id == row['run_id'])
                .values(status='finalizing')
            )

    monkeypatch.setattr(execution, 'check_operation', races)
    with pytest.raises(StaleExecution):
        await service.propose(
            execution, 'late-proposal', ref, row['scope'], {'service': 'payment-api'}
        )
    assert await platform.count_rows('actions') == 1


@pytest.mark.asyncio
async def test_action_policy_cli_sync_is_explicit_and_idempotent(platform, config_data, tmp_path):
    import json, subprocess, sys, yaml
    from uuid import uuid4

    grant = {
        'team_id': platform.auth.team_id,
        'agent_id': platform.agent_id,
        'binding': {'resource_id': str(uuid4()), 'version': '1', 'content_hash': 'a' * 64},
        'approver': platform.auth.subject,
        'arguments': {'service': 'payment-api'},
    }
    path = tmp_path / 'deployment.yaml'
    path.write_text(yaml.safe_dump({**config_data, 'service': {'action_grants': [grant]}}))

    def sync(revision):
        return subprocess.run(
            [
                sys.executable,
                '-m',
                'workbench_agent.cli.main',
                'action-policy',
                'sync',
                '--config',
                str(path),
                '--expected-revision',
                str(revision),
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=20,
        )

    first = await __import__('asyncio').to_thread(sync, 0)
    second = await __import__('asyncio').to_thread(sync, 1)
    assert json.loads(first.stdout) == {'ready': True, 'revision': 1}
    assert json.loads(second.stdout) == {'ready': True, 'revision': 1}
    async with platform.backend.create_uow().begin_read() as uow:
        policy = await platform.backend.actions.policy(uow)
    assert json.loads(policy['policy_json'])['grants'] == [grant]


@pytest.mark.asyncio
async def test_process_policy_revoke_blocks_separate_worker_with_stale_config(
    platform, config_data, tmp_path
):
    import asyncio, json, subprocess, sys, yaml
    from workbench_agent.delivery.dispatcher import Dispatcher
    from workbench_agent.persistence.backends.sqlite import SQLiteBackend

    service, action = await setup_action(platform)
    await service.authorize(platform.auth, action['approval_id'], 'approve', action['payload_hash'])
    worker = SQLiteBackend(
        platform.settings.storage.database_url.get_secret_value(),
        tool_permissions=platform.backend._tool_permissions,
    )
    worker._action_grants = list(platform.backend._action_grants)

    class Sender:
        calls = 0

        async def send(self, *args):
            self.calls += 1

    sender = Sender()
    worker._delivery_senders = {'ticket': sender}
    path = tmp_path / 'revoke.yaml'
    path.write_text(yaml.safe_dump(config_data))

    def sync():
        return subprocess.run(
            [
                sys.executable,
                '-m',
                'workbench_agent.cli.main',
                'action-policy',
                'sync',
                '--config',
                str(path),
                '--expected-revision',
                '1',
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=20,
        )

    try:
        result = await asyncio.to_thread(sync)
        assert json.loads(result.stdout) == {'ready': True, 'revision': 2}
        assert await Dispatcher(worker, 'worker').dispatch_one() == 'denied'
        assert sender.calls == 0
    finally:
        await worker.close()


@pytest.mark.asyncio
async def test_stale_maintenance_policy_sync_cannot_undo_revocation(platform):
    from workbench_agent.application.action_grants import ActionGrantService
    from workbench_agent.persistence.backends.sqlite import SQLiteBackend

    service, action = await setup_action(platform)
    policy = platform._test_action_policy
    other = SQLiteBackend(platform.settings.storage.database_url.get_secret_value())
    try:
        assert (
            await ActionGrantService(platform.backend).sync(
                [], policy['rules'], policy['connectors'], expected_revision=1
            )
            == 2
        )
        with pytest.raises(DomainError) as error:
            await ActionGrantService(other).sync(
                policy['grants'], policy['rules'], policy['connectors'], expected_revision=1
            )
        assert error.value.code == 'REVISION_CONFLICT'
        async with platform.backend.create_uow().begin_read() as uow:
            row = await platform.backend.actions.policy(uow)
        import json

        assert row['revision'] == 2 and json.loads(row['policy_json'])['grants'] == []
    finally:
        await other.close()


@pytest.mark.asyncio
async def test_policy_cli_status_and_stale_sync_are_explicit(platform, config_data, tmp_path):
    import asyncio, json, subprocess, sys, yaml

    path = tmp_path / 'policy-status.yaml'
    path.write_text(yaml.safe_dump(config_data))

    def call(*args):
        return subprocess.run(
            [
                sys.executable,
                '-m',
                'workbench_agent.cli.main',
                'action-policy',
                *args,
                '--config',
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )

    before = await asyncio.to_thread(call, 'status')
    assert json.loads(before.stdout) == {'ready': True, 'revision': 0, 'policy_hash': None}
    first = await asyncio.to_thread(call, 'sync', '--expected-revision', '0')
    assert json.loads(first.stdout) == {'ready': True, 'revision': 1}
    stale = await asyncio.to_thread(call, 'sync', '--expected-revision', '0')
    assert stale.returncode != 0 and json.loads(stale.stdout)['ready'] is False
    status = await asyncio.to_thread(call, 'status')
    assert json.loads(status.stdout)['revision'] == 1


@pytest.mark.asyncio
async def test_manual_retry_keeps_action_id_after_proven_unsent_exhaustion(platform):
    from workbench_agent.delivery.dispatcher import Dispatcher, DeliveryNotSent
    from sqlalchemy import select, update
    from workbench_agent.persistence import schema

    service, action = await setup_action(platform)
    await service.authorize(platform.auth, action['approval_id'], 'approve', action['payload_hash'])

    class Sender:
        calls = 0

        async def send(self, *args):
            self.calls += 1
            if self.calls <= 8:
                raise DeliveryNotSent()
            return {'external_id': 'local-success'}

    sender = Sender()
    platform.backend._delivery_senders = {'ticket': sender}
    dispatcher = Dispatcher(platform.backend, 'worker')
    for i in range(8):
        await dispatcher.dispatch_one()
        if i < 7:
            async with platform.backend.create_uow().begin_write() as uow:
                await uow.execute(
                    update(schema.outbox)
                    .where(schema.outbox.c.action_id == action['action_id'])
                    .values(available_at=0)
                )
    first = await service.retry(platform.auth, action['action_id'], key='retry-1')
    second = await service.retry(platform.auth, action['action_id'], key='retry-1')
    assert first == second and first['action_id'] == action['action_id']
    assert await dispatcher.dispatch_one() == 'succeeded'
    assert sender.calls == 9
    assert await platform.count_rows('actions') == 1 and await platform.count_rows('outbox') == 1
    async with platform.backend.create_uow().begin_read() as uow:
        assert (await uow.execute(select(schema.outbox.c.attempt))).scalar_one() == 9


@pytest.mark.asyncio
async def test_manual_retry_never_resends_unknown(platform):
    from workbench_agent.delivery.dispatcher import Dispatcher

    service, action = await setup_action(platform)
    await service.authorize(platform.auth, action['approval_id'], 'approve', action['payload_hash'])

    class Sender:
        calls = 0

        async def send(self, *args):
            self.calls += 1
            raise TimeoutError()

    sender = Sender()
    platform.backend._delivery_senders = {'ticket': sender}
    assert await Dispatcher(platform.backend, 'worker').dispatch_one() == 'unknown'
    with pytest.raises(DomainError):
        await service.retry(platform.auth, action['action_id'], key='retry-unknown')
    assert sender.calls == 1


@pytest.mark.asyncio
async def test_manual_retry_http_is_idempotent_and_requires_live_approval(platform, client):
    from workbench_agent.delivery.dispatcher import Dispatcher, DeliveryNotSent
    from sqlalchemy import update
    from workbench_agent.persistence import schema

    service, action = await setup_action(platform)
    await service.authorize(platform.auth, action['approval_id'], 'approve', action['payload_hash'])

    class Sender:
        async def send(self, *args):
            raise DeliveryNotSent()

    platform.backend._delivery_senders = {'ticket': Sender()}
    dispatcher = Dispatcher(platform.backend, 'worker')
    for i in range(8):
        await dispatcher.dispatch_one()
        if i < 7:
            async with platform.backend.create_uow().begin_write() as uow:
                await uow.execute(
                    update(schema.outbox)
                    .where(schema.outbox.c.action_id == action['action_id'])
                    .values(available_at=0)
                )
    path = f'/v1/actions/{action["action_id"]}/retry'
    first = await client.post(path, headers={'Idempotency-Key': 'manual-1'})
    assert first.status_code == 202 and first.json()['action_id'] == action['action_id']
    second = await client.post(path, headers={'Idempotency-Key': 'manual-1'})
    assert second.status_code == 202 and second.json() == first.json()
    assert await platform.count_rows('outbox') == 1
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(
            update(schema.approvals)
            .where(schema.approvals.c.approval_id == action['approval_id'])
            .values(expires_at=0)
        )
    # Idempotent replay returns the committed receipt; a new attempt cannot bypass expiry.
    assert (await client.post(path, headers={'Idempotency-Key': 'manual-1'})).status_code == 202
    assert (await client.post(path, headers={'Idempotency-Key': 'manual-2'})).status_code == 409


@pytest.mark.asyncio
async def test_manual_retry_is_denied_after_policy_revocation(platform):
    from workbench_agent.delivery.dispatcher import Dispatcher, DeliveryNotSent
    from sqlalchemy import update
    from workbench_agent.persistence import schema

    service, action = await setup_action(platform)
    await service.authorize(platform.auth, action['approval_id'], 'approve', action['payload_hash'])

    class Sender:
        async def send(self, *args):
            raise DeliveryNotSent()

    platform.backend._delivery_senders = {'ticket': Sender()}
    dispatcher = Dispatcher(platform.backend, 'worker')
    for i in range(8):
        await dispatcher.dispatch_one()
        if i < 7:
            async with platform.backend.create_uow().begin_write() as uow:
                await uow.execute(
                    update(schema.outbox)
                    .where(schema.outbox.c.action_id == action['action_id'])
                    .values(available_at=0)
                )
    await set_policy(platform, grants=[])
    with pytest.raises(DomainError):
        await service.retry(platform.auth, action['action_id'], key='manual-denied')


@pytest.mark.asyncio
async def test_manual_retry_rollback_preserves_failed_action(platform, monkeypatch):
    from workbench_agent.delivery.dispatcher import Dispatcher, DeliveryNotSent
    from sqlalchemy import update, select
    from workbench_agent.persistence import schema

    service, action = await setup_action(platform)
    await service.authorize(platform.auth, action['approval_id'], 'approve', action['payload_hash'])

    class Sender:
        async def send(self, *args):
            raise DeliveryNotSent()

    platform.backend._delivery_senders = {'ticket': Sender()}
    dispatcher = Dispatcher(platform.backend, 'worker')
    for i in range(8):
        await dispatcher.dispatch_one()
        if i < 7:
            async with platform.backend.create_uow().begin_write() as uow:
                await uow.execute(
                    update(schema.outbox)
                    .where(schema.outbox.c.action_id == action['action_id'])
                    .values(available_at=0)
                )

    async def fail(*args, **kwargs):
        raise RuntimeError('rollback')

    monkeypatch.setattr(platform.backend.execution_events, 'append_control', fail)
    with pytest.raises(RuntimeError):
        await service.retry(platform.auth, action['action_id'], key='rollback')
    async with platform.backend.create_uow().begin_read() as uow:
        state = (
            await uow.execute(
                select(schema.actions.c.state).where(
                    schema.actions.c.action_id == action['action_id']
                )
            )
        ).scalar_one()
        outbox = (
            (
                await uow.execute(
                    select(schema.outbox).where(schema.outbox.c.action_id == action['action_id'])
                )
            )
            .mappings()
            .one()
        )
    assert state == 'failed' and outbox['status'] == 'dead' and outbox['attempt'] == 8


@pytest.mark.asyncio
async def test_expired_approval_reapproved_without_changing_action_or_outbox(platform, monkeypatch):
    from workbench_agent.delivery.dispatcher import Dispatcher, DeliveryNotSent
    from sqlalchemy import select
    from workbench_agent.persistence import schema

    service, action = await setup_action(platform)
    await service.authorize(platform.auth, action['approval_id'], 'approve', action['payload_hash'])
    async with platform.backend.create_uow().begin_read() as uow:
        now = [await platform.backend.clock.now_ms(uow)]

    async def clock(_uow):
        return now[0]

    monkeypatch.setattr(platform.backend.clock, 'now_ms', clock)

    class Sender:
        calls = 0

        async def send(self, *args):
            self.calls += 1
            if self.calls <= 6:
                raise DeliveryNotSent()
            return {'external_id': 'local-after-reapproval'}

    sender = Sender()
    platform.backend._delivery_senders = {'ticket': sender}
    dispatcher = Dispatcher(platform.backend, 'worker')
    for delay in (0, 2000, 10000, 30000, 120000, 600000):
        now[0] += delay
        assert await dispatcher.dispatch_one() == 'retry'
    now[0] += 1800000
    assert await dispatcher.dispatch_one() == 'denied'
    assert sender.calls == 6
    async with platform.backend.create_uow().begin_read() as uow:
        old = (
            (
                await uow.execute(
                    select(schema.outbox).where(schema.outbox.c.action_id == action['action_id'])
                )
            )
            .mappings()
            .one()
        )
    pending = await service.reapprove(
        platform.auth, action['action_id'], action['payload_hash'], key='renew-1'
    )
    assert (
        pending['approval_id'] != action['approval_id'] and pending['state'] == 'reapproval_pending'
    )
    assert (
        await service.reapprove(
            platform.auth, action['action_id'], action['payload_hash'], key='renew-1'
        )
        == pending
    )
    approved = await service.authorize(
        platform.auth, pending['approval_id'], 'approve', action['payload_hash']
    )
    assert approved['state'] == 'authorized'
    assert await dispatcher.dispatch_one() == 'succeeded'
    assert sender.calls == 7
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
        approvals = (
            (
                await uow.execute(
                    select(schema.approvals).where(
                        schema.approvals.c.action_id == action['action_id']
                    )
                )
            )
            .mappings()
            .all()
        )
    assert box['outbox_id'] == old['outbox_id'] and box['attempt'] == 7
    assert len(approvals) == 2 and {x['approval_id'] for x in approvals} == {
        action['approval_id'],
        pending['approval_id'],
    }


@pytest.mark.asyncio
async def test_reapproval_never_revives_unknown_or_changed_grant(platform):
    from workbench_agent.delivery.dispatcher import Dispatcher

    service, action = await setup_action(platform)
    await service.authorize(platform.auth, action['approval_id'], 'approve', action['payload_hash'])

    class Sender:
        async def send(self, *args):
            raise TimeoutError()

    platform.backend._delivery_senders = {'ticket': Sender()}
    assert await Dispatcher(platform.backend, 'worker').dispatch_one() == 'unknown'
    with pytest.raises(DomainError):
        await service.reapprove(
            platform.auth, action['action_id'], action['payload_hash'], key='renew-unknown'
        )
    await set_policy(platform, grants=[])
    with pytest.raises(DomainError):
        await service.reapprove(
            platform.auth, action['action_id'], action['payload_hash'], key='renew-revoked'
        )


@pytest.mark.asyncio
async def test_reapproval_http_is_named_idempotent_and_old_approval_is_immutable(platform, client):
    from sqlalchemy import select, update
    from workbench_agent.persistence import schema

    service, action = await setup_action(platform)
    await service.authorize(platform.auth, action['approval_id'], 'approve', action['payload_hash'])
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(
            update(schema.approvals)
            .where(schema.approvals.c.approval_id == action['approval_id'])
            .values(expires_at=0)
        )
        await uow.execute(
            update(schema.actions)
            .where(schema.actions.c.action_id == action['action_id'])
            .values(state='failed')
        )
        await uow.execute(
            update(schema.outbox)
            .where(schema.outbox.c.action_id == action['action_id'])
            .values(status='dead', attempt=6)
        )
    path = f'/v1/actions/{action["action_id"]}/reapproval'
    body = {'expected_args_hash': action['payload_hash']}
    first = await client.post(path, json=body, headers={'Idempotency-Key': 'renew-http'})
    assert first.status_code == 202 and first.json()['approval_id'] != action['approval_id']
    second = await client.post(path, json=body, headers={'Idempotency-Key': 'renew-http'})
    assert second.status_code == 202 and second.json() == first.json()
    details = await client.get(f'/v1/actions/{action["action_id"]}')
    assert (
        details.status_code == 200 and details.json()['approval_id'] == first.json()['approval_id']
    )
    old = await client.post(
        f'/v1/approvals/{action["approval_id"]}/decision', json={'decision': 'approve', **body}
    )
    assert old.status_code == 409
    wrong = await client.post(
        f'/v1/approvals/{first.json()["approval_id"]}/decision',
        json={'decision': 'approve', 'expected_args_hash': '0' * 64},
    )
    assert wrong.status_code == 409
    assert await platform.count_rows('outbox') == 1
    async with platform.backend.create_uow().begin_read() as uow:
        original = (
            (
                await uow.execute(
                    select(schema.approvals).where(
                        schema.approvals.c.approval_id == action['approval_id']
                    )
                )
            )
            .mappings()
            .one()
        )
    assert original['decision'] == 'approve' and original['expires_at'] == 0


@pytest.mark.asyncio
async def test_reapproval_is_refused_when_scope_changed_or_requester_not_named(platform):
    from sqlalchemy import select, update
    from workbench_agent.persistence import schema
    from workbench_agent.domain.identity import AuthContext

    service, action = await setup_action(platform)
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
        await uow.execute(
            update(schema.approvals)
            .where(schema.approvals.c.approval_id == action['approval_id'])
            .values(expires_at=0)
        )
        await uow.execute(
            update(schema.actions)
            .where(schema.actions.c.action_id == action['action_id'])
            .values(state='failed')
        )
        await uow.execute(
            update(schema.outbox)
            .where(schema.outbox.c.action_id == action['action_id'])
            .values(status='dead', attempt=6)
        )
    async with platform.backend.create_uow().begin_read() as uow:
        team = await platform.backend.configuration.one(uow, 'teams', team_id=platform.auth.team_id)
    await platform.teams.set_member(
        platform.auth, 'other-person', 'operator', 'active', team['revision'], key='another-person'
    )
    other = AuthContext(team_id=platform.auth.team_id, subject='other-person', roles=('operator',))
    with pytest.raises(DomainError):
        await service.reapprove(other, action['action_id'], action['payload_hash'], key='bad-human')
    async with platform.backend.create_uow().begin_write() as uow:
        await platform.contexts.query_scope(uow, platform.auth, row['run_id'], {'service': 'other'})
    with pytest.raises(DomainError):
        await service.reapprove(
            platform.auth, action['action_id'], action['payload_hash'], key='bad-scope'
        )
    assert await platform.count_rows('approvals') == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['approve', 'dispatch', 'reconcile'])
@pytest.mark.parametrize('revocation', ['identity', 'source'])
async def test_v13_action_cannot_use_another_connector_identity(platform, operation, revocation):
    import json
    from sqlalchemy import insert, select, update
    from workbench_agent.persistence import schema
    from workbench_agent.delivery.dispatcher import Dispatcher
    from workbench_agent.delivery.reconcile import ActionReconciler

    service, action = await setup_action(platform)
    if operation != 'approve':
        await service.authorize(
            platform.auth, action['approval_id'], 'approve', action['payload_hash']
        )
    from workbench_agent.application.synthetic_source_policy import SyntheticSourcePolicy

    grant = dict(
        team_id=platform.auth.team_id,
        agent_id=platform.agent_id,
        connector_id='source',
        identity_id='revoked-source',
        subject=platform.auth.subject,
        service='checkout',
        environment='test',
        region='id',
        rule='error-rate',
    )
    await SyntheticSourcePolicy(platform.backend).sync(
        [grant],
        connector_identities={
            'revoked-source': {
                'subject': platform.auth.subject,
                'team_id': platform.auth.team_id,
                'agent_ids': [platform.agent_id],
            }
        },
        expected_revision=0,
    )
    connector_key = 'another' if revocation == 'identity' else 'revoked-source'
    connectors = {
        connector_key: {
            'subject': platform.auth.subject,
            'team_id': platform.auth.team_id,
            'agent_ids': [platform.agent_id],
        }
    }
    await set_policy(platform, connectors=connectors)
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
        await uow.execute(
            insert(schema.synthetic_alert_sources).values(
                team_id=platform.auth.team_id,
                run_id=run['run_id'],
                connector_id='source',
                identity_id='revoked-source',
                payload=json.dumps(
                    {
                        'service': 'checkout',
                        'environment': 'test',
                        'region': 'id',
                        'rule': 'error-rate',
                    }
                ),
                payload_hash=__import__(
                    'workbench_agent.application.idempotency', fromlist=['digest']
                ).digest(
                    {
                        'service': 'checkout',
                        'environment': 'test',
                        'region': 'id',
                        'rule': 'error-rate',
                    }
                ),
                created_at=0,
            )
        )
        policy = await platform.backend.actions.policy(uow)
        from workbench_agent.application.idempotency import digest

        grant = platform.backend._action_grants[0]
        approval_hash = digest({'grant': grant, 'policy_revision': policy['revision']})
        await uow.execute(
            update(schema.approvals)
            .where(schema.approvals.c.approval_id == action['approval_id'])
            .values(policy_hash=approval_hash)
        )
        if operation == 'reconcile':
            await uow.execute(
                update(schema.actions)
                .where(schema.actions.c.action_id == action['action_id'])
                .values(state='unknown')
            )
            await uow.execute(
                update(schema.outbox)
                .where(schema.outbox.c.action_id == action['action_id'])
                .values(status='unknown')
            )
    if revocation == 'source':
        await SyntheticSourcePolicy(platform.backend).sync([], expected_revision=1)

    class Adapter:
        calls = 0

        async def send(self, *args):
            self.calls += 1
            return {'external_id': 'sent'}

        async def lookup(self, *args):
            self.calls += 1
            return {'external_id': 'sent'}

    adapter = Adapter()
    platform.backend._delivery_senders = {'ticket': adapter}
    if operation == 'dispatch':
        assert await Dispatcher(platform.backend, 'guarded').dispatch_one() == 'denied'
    elif operation == 'reconcile':
        with pytest.raises(DomainError):
            await ActionReconciler(platform.backend).reconcile(platform.auth, action['action_id'])
    else:
        with pytest.raises(DomainError):
            await service.authorize(
                platform.auth, action['approval_id'], 'approve', action['payload_hash']
            )
        assert await platform.count_rows('outbox') == 0
    assert adapter.calls == 0

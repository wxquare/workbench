"""Explicitly synthetic: no external service or messages."""

import hashlib
import hmac
import json
import time
from datetime import datetime, timezone, timedelta
from uuid import uuid4
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import insert, select
from workbench_agent.application.action_grants import ActionGrantService
from workbench_agent.application.synthetic_source_policy import SyntheticSourcePolicy
from workbench_agent.application.synthetic_proposal_policy import SyntheticProposalPolicy
from workbench_agent.api.app import create_app
from workbench_agent.domain.profiles import ProfileDraft
from workbench_agent.domain.tools import ToolBinding
from workbench_agent.ingress.alerts import mac_message
from workbench_agent.jobs.worker import Worker, DeploymentConnectorPolicy
from workbench_agent.runtime.graphs.registry import foundation_worker_registry
from workbench_agent.settings import SyntheticIngressBinding
from workbench_agent.tools.permissions import DeploymentToolPermissions
from workbench_agent.tools.registry import ToolRegistry
from workbench_agent.persistence import schema


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'scenario',
    [
        'normal',
        'crash',
        'early',
        'early_reject',
        'rule_revoked',
        'post_approval_revoked',
        'revoke_regrant',
        'source_regrant',
        'scope_changed',
        'source_revoked',
        'no_rule',
        'proposal_race',
    ],
)
async def test_signed_alert_worker_proposes_and_human_approves_local_delivery(
    platform, monkeypatch, tmp_path, scenario
):
    now = int(time.time())
    anchor = datetime.fromtimestamp(now, timezone.utc)
    start = anchor - timedelta(seconds=600)
    scope = {
        'service': 'checkout',
        'environment': 'test',
        'region': 'id',
        'start_at': start.isoformat(),
        'end_at': anchor.isoformat(),
    }
    binding = ToolBinding(
        alias='test.jira',
        provider='mcp',
        tool_name='CreateTicket',
        server='local.mock.jira',
        credential_scope='local-mock',
        permission='ticket.propose',
        read_only=False,
        input_schema={
            'type': 'object',
            'properties': {k: {'type': 'string'} for k in scope},
            'required': list(scope),
            'additionalProperties': False,
        },
    )
    ref = await ToolRegistry(platform.backend).publish(
        platform.auth, platform.agent_id, binding, key='journey-tool'
    )
    context = await platform.contexts.resolve(platform.auth, platform.agent_id)
    agent = await platform.profiles.get_agent(platform.auth, platform.agent_id)
    draft = await platform.profiles.save_draft(
        platform.auth,
        platform.agent_id,
        ProfileDraft(default_model=context.default_model, graph_id='rca.proposal', tools=(ref,)),
        agent['revision'],
        key='v14-draft',
    )
    await platform.profiles.publish(
        platform.auth, platform.agent_id, draft['revision'], key='v14-publish'
    )
    async with platform.backend.create_uow().begin_write() as uow:
        clock = await platform.backend.clock.now_ms(uow)
        await uow.execute(
            insert(schema.team_members).values(
                team_id=platform.auth.team_id,
                member_id=str(uuid4()),
                subject='approver',
                role='operator',
                status='active',
                created_at=clock,
                updated_at=clock,
            )
        )
    identities = {
        'source-id': {
            'subject': platform.auth.subject,
            'team_id': platform.auth.team_id,
            'agent_ids': [platform.agent_id],
        }
    }
    source = dict(
        team_id=platform.auth.team_id,
        agent_id=platform.agent_id,
        connector_id='alert',
        identity_id='source-id',
        subject=platform.auth.subject,
        service='checkout',
        environment='test',
        region='id',
        rule='error-rate',
    )
    await SyntheticSourcePolicy(platform.backend).sync(
        [source], connector_identities=identities, expected_revision=0
    )
    permissions = {
        'users': {platform.auth.subject: ['ticket.propose'], 'approver': ['ticket.propose']},
        'teams': {platform.auth.team_id: ['ticket.propose']},
        'agents': {platform.agent_id: ['ticket.propose']},
        'skills': {'__none__': ['ticket.propose']},
        'experts': {'diagnosis': ['ticket.propose']},
    }
    action_grant = {
        'team_id': platform.auth.team_id,
        'agent_id': platform.agent_id,
        'binding': ref.model_dump(mode='json'),
        'approver': 'approver',
        'arguments': scope,
    }
    await ActionGrantService(platform.backend).sync(
        [action_grant], permissions, identities, expected_revision=0
    )
    rules = (
        []
        if scenario == 'no_rule'
        else [
            {
                'source': source,
                'scope': scope,
                'binding': ref.model_dump(mode='json'),
                'arguments': scope,
                'approver': 'approver',
            }
        ]
    )
    await SyntheticProposalPolicy(platform.backend).sync(rules, expected_revision=0)
    platform.backend._tool_permissions = DeploymentToolPermissions(lambda: permissions)
    monkeypatch.setenv('V14_FAKE_KEY', 'fictional-test-only')
    ingress = SyntheticIngressBinding(
        protocol='synthetic-alert-v2',
        team_id=platform.auth.team_id,
        agent_id=platform.agent_id,
        actor_subject=platform.auth.subject,
        identity_id='source-id',
        secret_ref='env://V14_FAKE_KEY',
        allowed_scopes=[('checkout', 'test', 'id', 'error-rate')],
    )
    settings = platform.settings.model_copy(
        update={
            'environment': 'test',
            'service': platform.settings.service.model_copy(
                update={'synthetic_ingress': {'alert': ingress}, 'connector_identities': identities}
            ),
        }
    )
    app = create_app(settings, backend=platform.backend)
    event = {
        'event_id': str(uuid4()),
        'text': 'fictional alert',
        'region': 'id',
        'service': 'checkout',
        'environment': 'test',
        'rule': 'error-rate',
        'anchor': anchor.isoformat(),
        'window_seconds': 600,
        'occurred_at': now,
    }
    raw = json.dumps(event, sort_keys=True).encode()
    timestamp = str(now)
    sig = hmac.new(
        b'fictional-test-only',
        mac_message('synthetic-alert-v2', 'alert', timestamp, raw),
        hashlib.sha256,
    ).hexdigest()
    headers = {'x-dod-timestamp': timestamp, 'x-dod-signature': sig}
    auth = {'Authorization': f'Bearer development:{platform.auth.team_id}:approver'}
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            accepted = await client.post('/v1/ingress/alert', content=raw, headers=headers)
            assert accepted.status_code == 202, accepted.text
            run_id = accepted.json()['run_id']
            registry = foundation_worker_registry(platform.backend)
            policy = DeploymentConnectorPolicy(lambda: identities)
            if scenario == 'proposal_race':
                from workbench_agent.application.actions import ActionService

                original_propose = ActionService.propose

                async def revoke_before_write(self, *args, **kwargs):
                    await SyntheticProposalPolicy(platform.backend).sync([], expected_revision=1)
                    return await original_propose(self, *args, **kwargs)

                monkeypatch.setattr(ActionService, 'propose', revoke_before_write)
            if scenario in ('crash', 'early', 'early_reject'):
                from workbench_agent.application.actions import ActionService

                original = ActionService.propose_synthetic

                async def interrupted(self, execution, scope_id):
                    await original(self, execution, scope_id)
                    raise RuntimeError('crash after committed proposal')

                monkeypatch.setattr(ActionService, 'propose_synthetic', interrupted)
                assert (
                    await Worker(
                        platform.backend, 'v14-crash', registry, connector_policy=policy
                    ).run_once()
                    == 'recovery_scheduled'
                )
                monkeypatch.setattr(ActionService, 'propose_synthetic', original)
                if scenario in ('early', 'early_reject'):
                    async with platform.backend.create_uow().begin_read() as uow:
                        proposed = (
                            (
                                await uow.execute(
                                    select(schema.actions).where(schema.actions.c.run_id == run_id)
                                )
                            )
                            .mappings()
                            .one()
                        )
                        waiting = (
                            (
                                await uow.execute(
                                    select(schema.approvals).where(
                                        schema.approvals.c.run_id == run_id
                                    )
                                )
                            )
                            .mappings()
                            .one()
                        )
                    decision = await client.post(
                        '/v1/approvals/' + waiting['approval_id'] + '/decision',
                        headers=auth,
                        json={
                            'decision': 'reject' if scenario == 'early_reject' else 'approve',
                            'expected_args_hash': proposed['payload_hash'],
                        },
                    )
                    assert decision.status_code == 200
                    from workbench_agent.cli.local_dispatcher import dispatch_local_once

                    fake_path = tmp_path / 'early.sqlite3'
                    assert (await dispatch_local_once(settings, 'jira', fake_path))[
                        'status'
                    ] == 'idle'
                    assert not fake_path.exists()
                from workbench_agent.jobs.reconciler import Reconciler

                reconciler = Reconciler(platform.backend, registry)
                reconciler.connector_policy = policy
                assert (await reconciler.recover(run_id)).action == 'continue'
                assert (
                    await Worker(
                        platform.backend, 'v14-resume', registry, connector_policy=policy
                    ).run_once()
                    == 'projected'
                )
            else:
                assert (
                    await Worker(
                        platform.backend, 'v14-journey', registry, connector_policy=policy
                    ).run_once()
                    == 'projected'
                )
            async with platform.backend.create_uow().begin_read() as uow:
                actions = (
                    (
                        await uow.execute(
                            select(schema.actions).where(schema.actions.c.run_id == run_id)
                        )
                    )
                    .mappings()
                    .all()
                )
                approvals = (
                    (
                        await uow.execute(
                            select(schema.approvals).where(schema.approvals.c.run_id == run_id)
                        )
                    )
                    .mappings()
                    .all()
                )
            if scenario == 'early_reject':
                assert len(actions) == len(approvals) == 1 and actions[0]['state'] == 'rejected'
                assert await platform.count_rows('outbox') == 0
                return
            if scenario in ('no_rule', 'proposal_race'):
                assert not actions and not approvals and await platform.count_rows('outbox') == 0
                return
            assert len(actions) == len(approvals) == 1 and actions[0]['state'] == (
                'authorized' if scenario == 'early' else 'proposed'
            )
            assert await platform.count_rows('outbox') == (1 if scenario == 'early' else 0)
            stream = await client.get('/v1/runs/' + run_id + '/events', headers=auth)
            assert stream.status_code == 200
            messages = [
                json.loads(line[6:])
                for line in stream.text.splitlines()
                if line.startswith('data: ')
            ]
            events = [
                item
                for item in messages
                if item.get('type') == 'action.updated'
                and item.get('payload', {}).get('state') == 'proposed'
            ]
            assert len(events) == 1 and events[0]['payload']['action_id'] == actions[0]['action_id']
            action_response = await client.get(
                '/v1/actions/' + events[0]['payload']['action_id'], headers=auth
            )
            assert (
                action_response.status_code == 200
                and action_response.json()['approval_id'] == approvals[0]['approval_id']
            )
            if scenario == 'rule_revoked':
                await SyntheticProposalPolicy(platform.backend).sync([], expected_revision=1)
            if scenario == 'source_revoked':
                await SyntheticSourcePolicy(platform.backend).sync(
                    [], connector_identities=identities, expected_revision=1
                )
            if scenario == 'scope_changed':
                async with platform.backend.create_uow().begin_write() as uow:
                    await platform.contexts.query_scope(
                        uow, platform.auth, run_id, {**scope, 'region': 'other'}
                    )
            decision = await client.post(
                '/v1/approvals/' + approvals[0]['approval_id'] + '/decision',
                headers=auth,
                json={'decision': 'approve', 'expected_args_hash': actions[0]['payload_hash']},
            )
            if scenario in ('rule_revoked', 'source_revoked', 'scope_changed'):
                assert (
                    decision.status_code in (403, 409) and await platform.count_rows('outbox') == 0
                )
                return
            assert decision.status_code == 200, decision.text
            from workbench_agent.cli.local_dispatcher import dispatch_local_once

            if scenario == 'source_regrant':
                await SyntheticSourcePolicy(platform.backend).sync(
                    [], connector_identities=identities, expected_revision=1
                )
                await SyntheticSourcePolicy(platform.backend).sync(
                    [source], connector_identities=identities, expected_revision=2
                )
                assert (await dispatch_local_once(settings, 'jira', tmp_path / 'fake.sqlite3'))[
                    'status'
                ] == 'denied'
                assert not (tmp_path / 'fake.sqlite3').exists()
                return
            if scenario in ('post_approval_revoked', 'revoke_regrant'):
                await SyntheticProposalPolicy(platform.backend).sync([], expected_revision=1)
                if scenario == 'revoke_regrant':
                    await SyntheticProposalPolicy(platform.backend).sync(rules, expected_revision=2)
                assert (await dispatch_local_once(settings, 'jira', tmp_path / 'fake.sqlite3'))[
                    'status'
                ] == 'denied'
                assert not (tmp_path / 'fake.sqlite3').exists()
                return
            assert (await dispatch_local_once(settings, 'jira', tmp_path / 'fake.sqlite3'))[
                'status'
            ] == 'succeeded'
            incidents = (
                await client.get(
                    '/v1/incidents', params={'agent_id': platform.agent_id}, headers=auth
                )
            ).json()['items']
            incident = next(i for i in incidents if i['run_id'] == run_id)
            assert (
                len(incident['delivery_links']) == 1
                and incident['delivery_links'][0]['action_id'] == actions[0]['action_id']
            )

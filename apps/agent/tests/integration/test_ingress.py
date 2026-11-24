import hashlib
import hmac
import json
import time

import pytest

from workbench_agent.application.errors import DomainError
from workbench_agent.ingress.alerts import SyntheticAlertBinding, SyntheticAlertIngress
from integration.test_synthetic_alert_v2 import (
    event as alert_event,
    ingress as alert_ingress,
    publish_v13,
    signed as sign_alert,
)


def signed(body, secret=b'test-only-secret', timestamp=None, *, connector_id=None, legacy=False):
    body = {'occurred_at': int(time.time()), **body}
    raw = json.dumps(body, sort_keys=True).encode()
    timestamp = str(int(time.time()) if timestamp is None else timestamp)
    from workbench_agent.ingress.alerts import mac_message

    chat = 'thread_id' in body
    protocol = 'synthetic-chat-v1' if chat else 'synthetic-alert-v1'
    connector_id = connector_id or ('chat' if chat else 'test-alert')
    message = (
        timestamp.encode() + b'.' + raw
        if legacy
        else mac_message(protocol, connector_id, timestamp, raw)
    )
    signature = hmac.new(secret, message, hashlib.sha256).hexdigest()
    return raw, {'x-dod-timestamp': timestamp, 'x-dod-signature': signature}


def binding(platform):
    return SyntheticAlertBinding(
        connector_id='test-alert',
        team_id=platform.auth.team_id,
        agent_id=platform.agent_id,
        actor_subject=platform.auth.subject,
        secret=b'test-only-secret',
    )


async def test_duplicate_signed_alert_atomically_creates_one_run(platform):
    await publish_v13(platform)
    ingress = alert_ingress(platform)
    raw, headers = sign_alert(alert_event())
    first = await ingress.accept(raw, headers)
    second = await ingress.accept(raw, headers)
    assert first.run_id == second.run_id
    assert await platform.count_rows('runs') == 1
    assert await platform.count_rows('source_events') == 1
    assert await platform.count_rows('synthetic_alert_sources') == 1


async def test_invalid_signature_or_changed_replay_never_creates_new_run(platform):
    await publish_v13(platform)
    ingress = alert_ingress(platform)
    alert = alert_event()
    raw, headers = sign_alert(alert)
    with pytest.raises(DomainError):
        await ingress.accept(raw + b' ', headers)
    assert await platform.count_rows('runs') == 0
    await ingress.accept(raw, headers)
    with pytest.raises(DomainError):
        await ingress.accept(*sign_alert({**alert, 'text': 'Different'}))
    assert await platform.count_rows('runs') == 1


async def test_connector_event_dedupes_after_service_account_rotation(platform):
    from uuid import uuid4
    from dataclasses import replace
    from workbench_agent.application.synthetic_source_policy import SyntheticSourcePolicy
    from workbench_agent.ingress.alerts import SyntheticAlertV2Ingress

    await publish_v13(platform)
    async with platform.backend.create_uow().begin_write() as uow:
        now = await platform.backend.clock.now_ms(uow)
        await platform.backend.configuration.insert(
            uow,
            'team_members',
            dict(
                team_id=platform.auth.team_id,
                member_id=str(uuid4()),
                subject='rotated',
                role='admin',
                status='active',
                created_at=now,
                updated_at=now,
            ),
        )
    ingress = alert_ingress(platform)
    raw, headers = sign_alert(alert_event())
    first = await ingress.accept(raw, headers)
    identities = {
        'v2-test': {
            'subject': 'rotated',
            'team_id': platform.auth.team_id,
            'agent_ids': [platform.agent_id],
        }
    }
    grant = dict(
        team_id=platform.auth.team_id,
        agent_id=platform.agent_id,
        connector_id='v2-test',
        identity_id='v2-test',
        subject='rotated',
        service='checkout',
        environment='test',
        region='id',
        rule='error-rate',
    )
    await SyntheticSourcePolicy(platform.backend).sync(
        [grant], connector_identities=identities, expected_revision=1
    )
    rotated = SyntheticAlertV2Ingress(
        platform.backend, replace(ingress.binding, actor_subject='rotated')
    )
    second = await rotated.accept(raw, headers)
    assert first.run_id == second.run_id
    assert await platform.count_rows('runs') == 1


async def test_source_receipt_failure_rolls_back_run_and_job(platform, monkeypatch):
    await publish_v13(platform)
    original = platform.backend.runs.insert

    async def fail_receipt(uow, resource, values):
        if resource == 'source_events':
            raise RuntimeError('injected receipt failure')
        return await original(uow, resource, values)

    monkeypatch.setattr(platform.backend.runs, 'insert', fail_receipt)
    with pytest.raises(RuntimeError, match='injected receipt'):
        await alert_ingress(platform).accept(*sign_alert(alert_event()))
    assert await platform.count_rows('runs') == 0
    assert await platform.count_rows('run_jobs') == 0
    assert await platform.count_rows('source_events') == 0
    assert await platform.count_rows('synthetic_alert_sources') == 0


async def test_concurrent_duplicates_make_one_run(platform):
    import asyncio

    await publish_v13(platform)
    ingress = alert_ingress(platform)
    raw, headers = sign_alert(alert_event())
    first, second = await asyncio.gather(ingress.accept(raw, headers), ingress.accept(raw, headers))
    assert first.run_id == second.run_id
    assert await platform.count_rows('runs') == 1


@pytest.mark.asyncio
async def test_stale_signature_is_rejected_before_admission(platform):
    ingress = SyntheticAlertIngress(platform.backend, binding(platform))
    raw, headers = signed(
        {'event_id': 'old', 'text': 'Threshold', 'region': 'ID'}, timestamp=int(time.time()) - 301
    )
    with pytest.raises(DomainError) as error:
        await ingress.accept(raw, headers)
    assert error.value.code == 'INGRESS_AUTH_FAILED'
    assert await platform.count_rows('runs') == 0


@pytest.mark.asyncio
async def test_old_alert_requires_explicit_backfill(platform):
    ingress = SyntheticAlertIngress(platform.backend, binding(platform))
    raw, headers = signed(
        {
            'event_id': 'old-event',
            'text': 'Threshold',
            'region': 'ID',
            'occurred_at': int(time.time()) - 7 * 24 * 3600 - 1,
        }
    )
    with pytest.raises(DomainError) as error:
        await ingress.accept(raw, headers)
    assert error.value.code == 'INGRESS_BACKFILL_REQUIRED'
    assert await platform.count_rows('runs') == 0


@pytest.mark.asyncio
async def test_chat_thread_survives_service_restart_and_replay(platform):
    from workbench_agent.ingress.seatalk import SyntheticChatBinding, SyntheticChatIngress

    chat_binding = SyntheticChatBinding(
        connector_id='chat',
        team_id=platform.auth.team_id,
        agent_id=platform.agent_id,
        actors={'external-alice': platform.auth.subject},
        secret=b'test-only-secret',
    )
    one, h1 = signed(
        {
            'event_id': 'm1',
            'thread_id': 'external-thread',
            'actor': 'external-alice',
            'text': 'first',
        }
    )
    two, h2 = signed(
        {
            'event_id': 'm2',
            'thread_id': 'external-thread',
            'actor': 'external-alice',
            'text': 'second',
        }
    )
    first = await SyntheticChatIngress(platform.backend, chat_binding).accept(one, h1)
    second = await SyntheticChatIngress(platform.backend, chat_binding).accept(two, h2)
    again = await SyntheticChatIngress(platform.backend, chat_binding).accept(two, h2)
    assert first.thread_id == second.thread_id == again.thread_id
    assert second.run_id == again.run_id and first.run_id != second.run_id
    assert await platform.count_rows('runs') == 2


@pytest.mark.asyncio
async def test_chat_does_not_infer_interrupt_and_unknown_actor_denied(platform):
    from workbench_agent.ingress.seatalk import SyntheticChatBinding, SyntheticChatIngress

    chat_binding = SyntheticChatBinding(
        connector_id='chat',
        team_id=platform.auth.team_id,
        agent_id=platform.agent_id,
        actors={'external-alice': platform.auth.subject},
        secret=b'test-only-secret',
    )
    raw, headers = signed(
        {'event_id': 'bad', 'thread_id': 'thread', 'actor': 'intruder', 'text': 'hi'}
    )
    with pytest.raises(DomainError):
        await SyntheticChatIngress(platform.backend, chat_binding).accept(raw, headers)
    assert await platform.count_rows('runs') == 0


@pytest.mark.asyncio
async def test_chat_named_resume_requires_actor_and_replays_atomically(platform, monkeypatch):
    from workbench_agent.ingress.seatalk import SyntheticChatBinding, SyntheticChatIngress
    from support.interrupts import InterruptPlatform
    from support.jobs import claim
    from workbench_agent.runtime.checkpoints.portable import (
        InvocationGuard,
        PortableCheckpointSaver,
    )
    from workbench_agent.persistence.checkpoints.codec import CheckpointCodec
    from support.interrupt_graph import build_interrupt_graph
    from workbench_agent.jobs.worker import Execution, DeploymentConnectorPolicy

    p = InterruptPlatform(platform)
    from uuid import uuid4

    async with platform.backend.create_uow().begin_write() as uow:
        now = await platform.backend.clock.now_ms(uow)
        await platform.backend.configuration.insert(
            uow,
            'team_members',
            dict(
                team_id=platform.auth.team_id,
                member_id=str(uuid4()),
                subject='wrong',
                role='operator',
                status='active',
                created_at=now,
                updated_at=now,
            ),
        )
    await p.publish_v2()
    chat_binding = SyntheticChatBinding(
        connector_id='chat',
        team_id=platform.auth.team_id,
        agent_id=platform.agent_id,
        actors={'alice': platform.auth.subject, 'wrong': 'wrong'},
        secret=b'test-only-secret',
    )
    ingress = SyntheticChatIngress(platform.backend, chat_binding)
    initial, h1 = signed(
        {'event_id': 'm1', 'thread_id': 't1', 'actor': 'alice', 'text': 'question'}
    )
    run = await ingress.accept(initial, h1)
    owned = await claim(platform)
    provider = InvocationGuard()
    graph = build_interrupt_graph(
        PortableCheckpointSaver(platform.backend, provider, CheckpointCodec())
    )
    with provider.bind(owned.guard):
        await graph.ainvoke(
            {
                'run_id': run.run_id,
                'input_text': 'question',
                'requires_input': True,
                'requires_approval': False,
                'actor': platform.auth.subject,
                'args_hash': 'a' * 64,
                'messages': [],
            },
            {'configurable': {'thread_id': run.run_id}},
            context=Execution(
                platform.backend,
                owned,
                DeploymentConnectorPolicy(
                    lambda: {
                        'chat': {
                            'subject': platform.auth.subject,
                            'team_id': platform.auth.team_id,
                            'agent_ids': [platform.agent_id],
                        }
                    }
                ),
            ),
            durability='sync',
        )
    async with platform.backend.create_uow().begin_write() as uow:
        await p.worker.registry._projectors['conversation.multi_turn'].project(uow, owned, None)
    view = await platform.get_run(run.run_id)
    event = {
        'event_id': 'm2',
        'thread_id': 't1',
        'actor': 'alice',
        'text': 'ID',
        'resume': {
            'run_id': run.run_id,
            'interrupt_id': view.waiting['interrupt_id'],
            'expected_revision': view.revision,
            'response': {'kind': 'user_input', 'values': {'region': 'ID'}},
        },
    }
    wrong, hwrong = signed(dict(event, actor='wrong'))
    with pytest.raises(DomainError) as error:
        await ingress.accept(wrong, hwrong)
    assert error.value.code == 'INTERRUPT_ACTOR_MISMATCH'
    wrong_thread, hthread = signed(dict(event, thread_id='other'))
    with pytest.raises(DomainError):
        await ingress.accept(wrong_thread, hthread)
    assert await p.resume_request_count(view.waiting['interrupt_id']) == 0
    raw, headers = signed(event)
    original = platform.backend.runs.insert

    async def fail_receipt(uow, resource, values):
        if resource == 'source_events':
            raise RuntimeError('resume receipt failure')
        return await original(uow, resource, values)

    with monkeypatch.context() as patch:
        patch.setattr(platform.backend.runs, 'insert', fail_receipt)
        with pytest.raises(RuntimeError, match='resume receipt failure'):
            await ingress.accept(raw, headers)
    assert await p.resume_request_count(view.waiting['interrupt_id']) == 0
    assert (await platform.get_run(run.run_id)).waiting['interrupt_id'] == view.waiting[
        'interrupt_id'
    ]
    first = await ingress.accept(raw, headers)
    second = await ingress.accept(raw, headers)
    assert first.run_id == second.run_id == run.run_id
    assert await p.resume_request_count(view.waiting['interrupt_id']) == 1
    changed, hchanged = signed(dict(event, text='changed', occurred_at=int(time.time())))
    with pytest.raises(DomainError):
        await ingress.accept(changed, hchanged)


@pytest.mark.asyncio
async def test_chat_duplicate_moved_to_another_thread_is_conflict(platform):
    from workbench_agent.ingress.seatalk import SyntheticChatBinding, SyntheticChatIngress

    chat_binding = SyntheticChatBinding(
        connector_id='chat',
        team_id=platform.auth.team_id,
        agent_id=platform.agent_id,
        actors={'alice': platform.auth.subject},
        secret=b'test-only-secret',
    )
    ingress = SyntheticChatIngress(platform.backend, chat_binding)
    event = {
        'event_id': 'm1',
        'thread_id': 'one',
        'actor': 'alice',
        'text': 'question',
        'occurred_at': int(time.time()),
    }
    one, h1 = signed(event)
    await ingress.accept(one, h1)
    two, h2 = signed(dict(event, thread_id='other'))
    with pytest.raises(DomainError):
        await ingress.accept(two, h2)
    assert await platform.count_rows('runs') == 1


@pytest.mark.asyncio
async def test_synthetic_http_requires_explicit_development_binding(platform, monkeypatch):
    from httpx import ASGITransport, AsyncClient
    from workbench_agent.api.app import create_app
    from workbench_agent.settings import SyntheticIngressBinding

    monkeypatch.setenv('TEST_INGRESS_KEY', 'test-only-secret')
    binding = SyntheticIngressBinding(
        protocol='synthetic-chat-v1',
        team_id=platform.auth.team_id,
        agent_id=platform.agent_id,
        actors={'alice': platform.auth.subject},
        secret_ref='env://TEST_INGRESS_KEY',
    )
    service = platform.settings.service.model_copy(
        update={
            'synthetic_ingress': {'local-chat': binding},
            'connector_identities': {
                'chat-actor': {
                    'subject': platform.auth.subject,
                    'team_id': platform.auth.team_id,
                    'agent_ids': [platform.agent_id],
                }
            },
        }
    )
    settings = platform.settings.model_copy(update={'service': service, 'environment': 'test'})
    app = create_app(settings, backend=platform.backend)
    raw, headers = signed(
        {'event_id': 'http1', 'thread_id': 't1', 'actor': 'alice', 'text': 'question'},
        connector_id='local-chat',
    )
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post('/v1/ingress/local-chat', content=raw, headers=headers)
            assert response.status_code == 202
            assert (
                await client.post('/v1/ingress/unconfigured', content=raw, headers=headers)
            ).status_code == 404
    assert await platform.count_rows('runs') == 1


def test_synthetic_binding_rejects_unregistered_worker_subject(platform):
    from workbench_agent.api.app import create_app
    from workbench_agent.settings import SyntheticIngressBinding

    binding = SyntheticIngressBinding(
        protocol='synthetic-chat-v1',
        team_id=platform.auth.team_id,
        agent_id=platform.agent_id,
        actors={'alice': platform.auth.subject},
        secret_ref='env://TEST_INGRESS_KEY',
    )
    settings = platform.settings.model_copy(
        update={
            'service': platform.settings.service.model_copy(
                update={'synthetic_ingress': {'local-chat': binding}, 'connector_identities': {}}
            ),
            'environment': 'test',
        }
    )
    with pytest.raises(ValueError, match='connector identity'):
        create_app(settings, backend=platform.backend)


def test_synthetic_ingress_forbidden_in_production(platform):
    from workbench_agent.api.app import create_app
    from workbench_agent.settings import SyntheticIngressBinding

    binding = SyntheticIngressBinding(
        protocol='synthetic-chat-v1',
        team_id=platform.auth.team_id,
        agent_id=platform.agent_id,
        actors={'alice': platform.auth.subject},
        secret_ref='env://TEST_INGRESS_KEY',
    )
    settings = platform.settings.model_copy(
        update={
            'service': platform.settings.service.model_copy(
                update={'synthetic_ingress': {'local-chat': binding}}
            ),
            'environment': 'production',
        }
    )
    with pytest.raises(ValueError, match='synthetic ingress'):
        create_app(settings, backend=platform.backend)


@pytest.mark.asyncio
async def test_non_ascii_signature_is_auth_failure(platform):
    ingress = SyntheticAlertIngress(platform.backend, binding(platform))
    raw, headers = signed({'event_id': 'bad-signature', 'text': 'test', 'region': 'ID'})
    headers['x-dod-signature'] = 'é' * 64
    with pytest.raises(DomainError) as error:
        await ingress.accept(raw, headers)
    assert error.value.code == 'INGRESS_AUTH_FAILED'


@pytest.mark.asyncio
async def test_http_signature_cannot_be_forwarded_between_connectors(platform, monkeypatch):
    from httpx import ASGITransport, AsyncClient
    from workbench_agent.api.app import create_app
    from workbench_agent.settings import SyntheticIngressBinding

    monkeypatch.setenv('TEST_INGRESS_KEY', 'test-only-secret')
    async with platform.backend.create_uow().begin_read() as uow:
        other_agents = await platform.backend.configuration.rows(
            uow, 'agents', team_id=platform.other_auth.team_id
        )
    other_agent = other_agents[0]['agent_id']

    def config(auth, agent):
        return SyntheticIngressBinding(
            protocol='synthetic-chat-v1',
            team_id=auth.team_id,
            agent_id=agent,
            actors={'alice': auth.subject},
            secret_ref='env://TEST_INGRESS_KEY',
        )

    service = platform.settings.service.model_copy(
        update={
            'synthetic_ingress': {
                'local-chat': config(platform.auth, platform.agent_id),
                'other-chat': config(platform.other_auth, other_agent),
            },
            'connector_identities': {
                'a': {
                    'subject': platform.auth.subject,
                    'team_id': platform.auth.team_id,
                    'agent_ids': [platform.agent_id],
                },
                'b': {
                    'subject': platform.other_auth.subject,
                    'team_id': platform.other_auth.team_id,
                    'agent_ids': [other_agent],
                },
            },
        }
    )
    app = create_app(
        platform.settings.model_copy(update={'service': service, 'environment': 'test'}),
        backend=platform.backend,
    )
    raw, headers = signed(
        {'event_id': 'cross', 'thread_id': 't', 'actor': 'alice', 'text': 'question'},
        connector_id='local-chat',
    )
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            assert (
                await client.post('/v1/ingress/local-chat', content=raw, headers=headers)
            ).status_code == 202
            assert (
                await client.post('/v1/ingress/other-chat', content=raw, headers=headers)
            ).status_code == 401
    assert await platform.count_rows('runs') == 1


@pytest.mark.asyncio
async def test_legacy_unbound_signature_is_rejected(platform):
    ingress = SyntheticAlertIngress(platform.backend, binding(platform))
    raw, headers = signed({'event_id': 'legacy', 'text': 'test', 'region': 'ID'}, legacy=True)
    with pytest.raises(DomainError) as error:
        await ingress.accept(raw, headers)
    assert error.value.code == 'INGRESS_AUTH_FAILED'


def test_mac_protocol_versions_are_domain_separated():
    from workbench_agent.ingress.alerts import verify_signed

    raw, headers = signed({'event_id': 'same', 'text': 'test', 'region': 'ID'})
    with pytest.raises(DomainError) as error:
        verify_signed(
            raw,
            headers,
            b'test-only-secret',
            protocol='synthetic-chat-v1',
            connector_id='test-alert',
        )
    assert error.value.code == 'INGRESS_AUTH_FAILED'

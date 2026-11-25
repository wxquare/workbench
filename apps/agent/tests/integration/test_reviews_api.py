import base64
import json

import pytest
from support.runs import make_run_payload


def _encode(value):
    return (
        base64.urlsafe_b64encode(json.dumps(value, separators=(',', ':')).encode())
        .decode()
        .rstrip('=')
    )


@pytest.mark.asyncio
async def test_reviews_endpoints_are_authorized_and_empty_without_items(client):
    response = await client.get('/v1/reviews')
    assert response.status_code == 200
    assert response.json()['items'] == []
    assert response.json()['next_cursor'] is None

    summary = await client.get('/v1/reviews/summary')
    assert summary.status_code == 200
    assert summary.json()['total'] == 0
    assert summary.json()['by_type'] == {
        'run_input': 0,
        'run_approval': 0,
        'action_approval': 0,
        'action_reapproval': 0,
        'action_reconcile': 0,
    }


@pytest.mark.asyncio
async def test_review_rejects_a_cursor_without_the_access_prefix(client, platform):
    response = await client.post(
        '/v1/runs',
        json=make_run_payload(platform.agent_id),
        headers={'Idempotency-Key': 'cursor-prefix'},
    )
    assert response.status_code == 202
    forged = _encode(
        {
            'version': 1,
            'team_id': platform.auth.team_id,
            'subject': platform.auth.subject,
            'state': 'pending',
            'type': None,
            'after': [1, 0, 0, ''],
        }
    )
    response = await client.get('/v1/reviews', params={'after': forged})
    assert response.status_code == 422
    assert response.json()['code'] == 'INVALID_CURSOR'


@pytest.mark.asyncio
async def test_reviews_requires_authentication(client):
    assert (await client.get('/v1/reviews', headers={'Authorization': ''})).status_code == 401
    assert (
        await client.get('/v1/reviews/summary', headers={'Authorization': ''})
    ).status_code == 401


@pytest.mark.asyncio
async def test_reviews_do_not_hide_existing_incident_api(client, platform):
    response = await client.get('/v1/incidents', params={'agent_id': platform.agent_id})
    assert response.status_code == 200
    assert 'items' in response.json()


@pytest.mark.asyncio
async def test_review_cursor_is_bound_to_filter_and_rejects_tampering(client, platform):
    response = await client.post(
        '/v1/runs',
        json={
            'agent_id': platform.agent_id,
            'mode': 'qa',
            'input': {'text': 'inspect'},
        },
        headers={'Idempotency-Key': 'review-cursor'},
    )
    assert response.status_code == 202
    cursor = _encode({'version': 1, 'state': 'pending', 'type': None})
    invalid = await client.get('/v1/reviews', params={'after': cursor, 'type': 'run_input'})
    assert invalid.status_code == 422
    assert invalid.json()['code'] == 'INVALID_CURSOR'


@pytest.mark.asyncio
async def test_review_list_and_summary_match_after_action_requires_human_review(client, platform):
    from test_action_contract import setup_action

    _, action = await setup_action(platform)
    pending = await client.get('/v1/reviews')
    assert pending.status_code == 200
    assert {
        item['item_id'] for item in pending.json()['items'] if item['type'] == 'action_approval'
    } == {f'action_approval:{action["action_id"]}'}
    summary = await client.get('/v1/reviews/summary')
    assert summary.status_code == 200
    assert summary.json()['by_type']['action_approval'] == 1

    details = await client.get(f'/v1/reviews/action_approval:{action["action_id"]}')
    assert details.status_code == 200
    assert details.json()['detail']['payload_hash'] == action['payload_hash']
    assert details.json()['detail']['arguments'] == {'service': 'payment-api'}

    approved = await client.post(
        f'/v1/approvals/{action["approval_id"]}/decision',
        json={
            'decision': 'approve',
            'expected_args_hash': action['payload_hash'],
        },
    )
    assert approved.status_code == 200
    after = await client.get('/v1/reviews')
    assert all(item['action_id'] != action['action_id'] for item in after.json()['items'])


@pytest.mark.asyncio
async def test_review_approval_for_a_different_human_is_not_visible(client, platform):
    from workbench_agent.tools.registry import ToolRegistry
    from workbench_agent.domain.tools import ToolBinding
    from workbench_agent.domain.profiles import ProfileDraft
    from support.skills import execution_for_run
    from workbench_agent.application.actions import ActionService
    from workbench_agent.tools.permissions import DeploymentToolPermissions
    from test_action_contract import set_policy

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
        platform.auth, platform.agent_id, binding, key='review-after-row'
    )
    context = await platform.contexts.resolve(platform.auth, platform.agent_id)
    agent = await platform.profiles.get_agent(platform.auth, platform.agent_id)
    changed = await platform.profiles.save_draft(
        platform.auth,
        platform.agent_id,
        ProfileDraft(default_model=context.default_model, tools=(ref,)),
        agent['revision'],
        key='review-after-draft',
    )
    await platform.profiles.publish(
        platform.auth,
        platform.agent_id,
        changed['revision'],
        key='review-after-publish',
    )
    async with platform.backend.create_uow().begin_read() as uow:
        team = await platform.backend.configuration.one(uow, 'teams', team_id=platform.auth.team_id)
    await platform.teams.set_member(
        platform.auth,
        'second-operator',
        'operator',
        'active',
        team['revision'],
        key='review-second',
    )
    run = await platform.create_run(key='review-after-run')
    async with platform.backend.create_uow().begin_write() as uow:
        scope = await platform.contexts.query_scope(
            uow, platform.auth, run.run_id, {'service': 'payment-api'}
        )
    rules = {
        'users': {
            platform.auth.subject: ['ticket.propose'],
            'second-operator': ['ticket.propose'],
        },
        'teams': {platform.auth.team_id: ['ticket.propose']},
        'agents': {platform.agent_id: ['ticket.propose']},
        'skills': {'__none__': ['ticket.propose']},
        'experts': {'diagnosis': ['ticket.propose']},
    }
    platform.backend._tool_permissions = DeploymentToolPermissions(lambda: rules)
    platform.backend._action_grants = [
        {
            'team_id': platform.auth.team_id,
            'agent_id': platform.agent_id,
            'binding': ref.model_dump(mode='json'),
            'approver': platform.auth.subject,
            'arguments': {'service': 'payment-api'},
        }
    ]
    await set_policy(platform, grants=platform.backend._action_grants, rules=rules)
    execution = await execution_for_run(platform, run)
    await ActionService(platform.backend).propose(
        execution,
        'different-approver',
        ref,
        scope['scope_id'],
        {'service': 'payment-api'},
    )

    other = await client.get(
        '/v1/reviews',
        headers={'Authorization': (f'Bearer development:{platform.auth.team_id}:second-operator')},
    )
    assert other.status_code == 200
    assert other.json()['items'] == []
    summary = await client.get(
        '/v1/reviews/summary',
        headers={'Authorization': (f'Bearer development:{platform.auth.team_id}:second-operator')},
    )
    assert summary.status_code == 200
    assert summary.json()['total'] == 0
    assert summary.json()['by_type']['action_approval'] == 0

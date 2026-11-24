import pytest


@pytest.mark.asyncio
async def test_reconcile_api_uses_lookup_and_never_resends(platform, client):
    from test_action_contract import setup_action
    from workbench_agent.delivery.dispatcher import Dispatcher

    service, action = await setup_action(platform)
    await service.authorize(platform.auth, action['approval_id'], 'approve', action['payload_hash'])

    class Connector:
        sends = 0
        lookups = 0

        async def send(self, *args):
            self.sends += 1
            raise TimeoutError()

        async def lookup(self, payload, action_id):
            self.lookups += 1
            return {'external_id': 'ticket-123'}

    connector = Connector()
    platform.backend._delivery_senders = {'ticket': connector}
    assert await Dispatcher(platform.backend, 'worker').dispatch_one() == 'unknown'
    path = f'/v1/actions/{action["action_id"]}/reconcile'
    first = await client.post(path, headers={'Idempotency-Key': 'reconcile-1'})
    assert first.status_code == 202
    assert first.json()['state'] == 'succeeded'
    second = await client.post(path, headers={'Idempotency-Key': 'reconcile-1'})
    assert second.status_code == 202
    assert second.json() == first.json()
    assert connector.sends == 1


@pytest.mark.asyncio
async def test_reconcile_api_requires_idempotency_key(platform, client):
    from test_action_contract import setup_action

    _, action = await setup_action(platform)
    response = await client.post(f'/v1/actions/{action["action_id"]}/reconcile')
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_reconcile_api_rejects_non_unknown_action(platform, client):
    from test_action_contract import setup_action

    _, action = await setup_action(platform)
    response = await client.post(
        f'/v1/actions/{action["action_id"]}/reconcile',
        headers={'Idempotency-Key': 'not-unknown'},
    )
    assert response.status_code == 409
    assert response.json()['code'] == 'ACTION_NOT_UNKNOWN'


@pytest.mark.asyncio
async def test_reconcile_unknown_fact_remains_unknown(platform, client):
    from test_action_contract import setup_action
    from workbench_agent.delivery.dispatcher import Dispatcher

    service, action = await setup_action(platform)
    await service.authorize(platform.auth, action['approval_id'], 'approve', action['payload_hash'])

    class Connector:
        sends = 0

        async def send(self, *args):
            self.sends += 1
            raise TimeoutError()

        async def lookup(self, *args):
            return None

    connector = Connector()
    platform.backend._delivery_senders = {'ticket': connector}
    assert await Dispatcher(platform.backend, 'worker').dispatch_one() == 'unknown'
    response = await client.post(
        f'/v1/actions/{action["action_id"]}/reconcile',
        headers={'Idempotency-Key': 'unknown-result'},
    )
    assert response.status_code == 202
    assert response.json()['state'] == 'unknown'
    assert connector.sends == 1


@pytest.mark.asyncio
async def test_reconcile_revoked_authority_never_calls_lookup(platform, client):
    from test_action_contract import set_policy, setup_action
    from workbench_agent.delivery.dispatcher import Dispatcher
    from workbench_agent.tools.permissions import DeploymentToolPermissions

    service, action = await setup_action(platform)
    await service.authorize(platform.auth, action['approval_id'], 'approve', action['payload_hash'])

    class Connector:
        sends = 0
        lookups = 0

        async def send(self, *args):
            self.sends += 1
            raise TimeoutError()

        async def lookup(self, *args):
            self.lookups += 1
            return {'external_id': 'should-not-read'}

    connector = Connector()
    platform.backend._delivery_senders = {'ticket': connector}
    assert await Dispatcher(platform.backend, 'worker').dispatch_one() == 'unknown'
    platform.backend._tool_permissions = DeploymentToolPermissions(lambda: {})
    await set_policy(platform, rules={})
    response = await client.post(
        f'/v1/actions/{action["action_id"]}/reconcile',
        headers={'Idempotency-Key': 'revoked-reconcile'},
    )
    assert response.status_code in (403, 404)
    assert connector.lookups == 0


@pytest.mark.asyncio
async def test_reconcile_without_lookup_is_unavailable(platform, client):
    from test_action_contract import setup_action
    from workbench_agent.delivery.dispatcher import Dispatcher

    service, action = await setup_action(platform)
    await service.authorize(platform.auth, action['approval_id'], 'approve', action['payload_hash'])

    class Connector:
        async def send(self, *args):
            raise TimeoutError()

    platform.backend._delivery_senders = {'ticket': Connector()}
    assert await Dispatcher(platform.backend, 'worker').dispatch_one() == 'unknown'
    response = await client.post(
        f'/v1/actions/{action["action_id"]}/reconcile',
        headers={'Idempotency-Key': 'missing-lookup'},
    )
    assert response.status_code == 503
    assert response.json()['code'] == 'RECONCILIATION_UNAVAILABLE'

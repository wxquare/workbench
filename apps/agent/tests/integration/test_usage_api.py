"""Authorized usage queries preserve unknown values."""

import pytest
from support.jobs import claim


@pytest.mark.asyncio
async def test_usage_api_reports_pending_without_inventing_zero(client, platform):
    from workbench_agent.domain.usage import ReservationRequest

    run = await platform.create_run()
    guard = (await claim(platform)).guard
    async with platform.backend.create_uow().begin_write() as uow:
        await platform.budgets.reserve(
            guard,
            ReservationRequest(
                invocation_id='model-request', kind='model', model_attempts=1, token_upper_bound=100
            ),
            uow=uow,
        )
        await platform.budgets.mark_sent(guard, 'model-request', uow=uow)
    result = await client.get('/v1/usage', params={'run_id': str(run.run_id)})
    assert result.status_code == 200
    assert result.json()['usage_pending'] is True
    assert result.json()['cost_pending'] is True
    assert result.json()['total_tokens'] is None
    assert result.json()['amount_micros'] is None


@pytest.mark.asyncio
async def test_usage_api_rejects_other_team(client, platform):
    run = await platform.create_run()
    headers = {
        'Authorization': f'Bearer development:{platform.other_auth.team_id}:{platform.other_auth.subject}'
    }
    result = await client.get('/v1/usage', params={'run_id': str(run.run_id)}, headers=headers)
    assert result.status_code == 404

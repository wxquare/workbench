import pytest
from workbench_agent.application.errors import DomainError


@pytest.mark.asyncio
async def test_explicit_synthetic_proposal_rule_is_versioned_and_fail_closed(platform):
    from workbench_agent.application.synthetic_proposal_policy import SyntheticProposalPolicy

    rule = {
        'source': {
            'team_id': platform.auth.team_id,
            'agent_id': platform.agent_id,
            'connector_id': 'local',
            'identity_id': 'id1',
            'subject': platform.auth.subject,
            'service': 'checkout',
            'environment': 'test',
            'region': 'id',
            'rule': 'error-rate',
        },
        'scope': {
            'service': 'checkout',
            'environment': 'test',
            'region': 'id',
            'start_at': '2026-11-14T00:00:00+00:00',
            'end_at': '2026-11-14T00:10:00+00:00',
        },
        'binding': {
            'resource_id': '8ad892ba-bda0-4ebc-a603-c208af125985',
            'version': '1',
            'content_hash': 'a' * 64,
        },
        'arguments': {
            'service': 'checkout',
            'environment': 'test',
            'region': 'id',
            'start_at': '2026-11-14T00:00:00+00:00',
            'end_at': '2026-11-14T00:10:00+00:00',
        },
        'approver': 'human',
    }
    service = SyntheticProposalPolicy(platform.backend)
    assert await service.sync([rule], expected_revision=0) == 1
    with pytest.raises(DomainError):
        await service.sync([], expected_revision=0)
    assert await service.sync([], expected_revision=1) == 2


@pytest.mark.asyncio
async def test_rule_requires_exact_source_and_scope_and_unique_match(platform):
    from workbench_agent.application.synthetic_proposal_policy import (
        SyntheticProposalPolicy,
        matching_rule,
    )

    source = {
        'team_id': platform.auth.team_id,
        'agent_id': platform.agent_id,
        'connector_id': 'local',
        'identity_id': 'id1',
        'subject': platform.auth.subject,
        'service': 'checkout',
        'environment': 'test',
        'region': 'id',
        'rule': 'error-rate',
    }
    scope = {
        'service': 'checkout',
        'environment': 'test',
        'region': 'id',
        'start_at': 'start',
        'end_at': 'end',
    }
    rule = {
        'source': source,
        'scope': scope,
        'binding': {
            'resource_id': '8ad892ba-bda0-4ebc-a603-c208af125985',
            'version': '1',
            'content_hash': 'a' * 64,
        },
        'arguments': scope,
        'approver': 'human',
    }
    await SyntheticProposalPolicy(platform.backend).sync([rule], expected_revision=0)
    async with platform.backend.create_uow().begin_read() as uow:
        assert await matching_rule(uow, source, scope) == rule
        with pytest.raises(DomainError):
            await matching_rule(uow, {**source, 'rule': 'other'}, scope)
        with pytest.raises(DomainError):
            await matching_rule(uow, source, {**scope, 'region': 'other'})


@pytest.mark.asyncio
async def test_explicit_maintenance_reports_revision_and_rejects_stale(platform):
    from workbench_agent.cli.synthetic_proposal_policy import (
        sync_proposal_policy,
        proposal_policy_status,
    )

    assert (await proposal_policy_status(platform.settings))['revision'] == 0
    assert (await sync_proposal_policy(platform.settings, [], 0))['revision'] == 1
    with pytest.raises(DomainError):
        await sync_proposal_policy(platform.settings, [], 0)

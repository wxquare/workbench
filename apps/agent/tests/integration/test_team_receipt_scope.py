from uuid import UUID, uuid4
import pytest
from workbench_agent.domain.identity import AuthContext


async def add_lower_agent(platform):
    async with platform.backend.create_uow().begin_write() as uow:
        await platform.backend.configuration.insert(
            uow,
            'agents',
            {'team_id': platform.auth.team_id, 'agent_id': str(UUID(int=1)), 'name': 'lower-agent'},
        )


async def test_team_create_replay_survives_new_lower_agent(platform):
    first = await platform.teams.create_team(
        platform.auth, 'replay-target', key='same-key', receipt=True
    )
    await add_lower_agent(platform)
    replay = await platform.teams.create_team(
        platform.auth, 'replay-target', key='same-key', receipt=True
    )
    assert replay.replayed is True
    assert replay.body == first.body


async def test_member_update_replay_survives_new_lower_agent(platform):
    first = await platform.teams.set_member(
        platform.auth, 'new-viewer', 'viewer', 'active', 0, key='same-key', receipt=True
    )
    await add_lower_agent(platform)
    replay = await platform.teams.set_member(
        platform.auth, 'new-viewer', 'viewer', 'active', 0, key='same-key', receipt=True
    )
    assert replay.replayed is True
    assert replay.body == first.body


async def test_team_commands_do_not_require_a_agent(platform):
    team_id = str(uuid4())
    async with platform.backend.create_uow().begin_write() as uow:
        await platform.backend.configuration.insert(
            uow, 'teams', {'team_id': team_id, 'name': 'no-agents', 'status': 'active'}
        )
        await platform.backend.configuration.insert(
            uow,
            'team_members',
            {
                'team_id': team_id,
                'member_id': str(uuid4()),
                'subject': 'admin-without-agent',
                'role': 'admin',
                'status': 'active',
            },
        )
    auth = AuthContext(team_id=team_id, subject='admin-without-agent', roles=('admin',))
    first = await platform.teams.set_member(
        auth, 'viewer', 'viewer', 'active', 0, key='member', receipt=True
    )
    replay = await platform.teams.set_member(
        auth, 'viewer', 'viewer', 'active', 0, key='member', receipt=True
    )
    assert replay.replayed and replay.body == first.body

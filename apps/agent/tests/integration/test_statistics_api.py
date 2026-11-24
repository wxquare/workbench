import json
from uuid import uuid4

from sqlalchemy import insert

from workbench_agent.persistence import schema


START = '2026-11-20T00:00:00Z'
END = '2026-11-21T00:00:00Z'


async def seed_run(platform, status, *, offset, completed=None, agent_id=None):
    run_id = str(uuid4())
    thread_id = str(uuid4())
    async with platform.backend.create_uow().begin_write() as uow:
        now = 1_795_132_800_000 + offset
        context = await platform.contexts.resolve(
            platform.auth, agent_id or platform.agent_id, uow=uow
        )
        await uow.execute(
            insert(schema.threads).values(
                team_id=platform.auth.team_id,
                thread_id=thread_id,
                agent_id=agent_id or platform.agent_id,
                active_run_id=None,
                revision=0,
                created_at=now,
                updated_at=now,
            )
        )
        await uow.execute(
            insert(schema.runs).values(
                team_id=platform.auth.team_id,
                run_id=run_id,
                thread_id=thread_id,
                agent_id=agent_id or platform.agent_id,
                status=status,
                phase=None,
                runtime_profile_id=context.agent_profile.resource_id,
                versions='{}',
                budgets='{}',
                revision=1,
                last_event_seq=0,
                wall_deadline_at=now + 10_000,
                created_at=now,
                updated_at=now,
                started_at=None,
                active_elapsed_ms=100,
            )
        )
        message_id = str(uuid4())
        await uow.execute(
            insert(schema.messages).values(
                team_id=platform.auth.team_id,
                message_id=message_id,
                thread_id=thread_id,
                run_id=run_id,
                role='assistant',
                content_ref='{}',
                created_at=now,
                updated_at=now,
            )
        )
        if completed is not None:
            await uow.execute(
                insert(schema.run_outcomes).values(
                    team_id=platform.auth.team_id,
                    run_id=run_id,
                    result_id=str(uuid4()),
                    digest='a' * 64,
                    payload=json.dumps({'completed': completed}),
                    message_id=message_id,
                    response=None,
                    created_at=now,
                )
            )
    return run_id


async def test_statistics_uses_terminal_denominator_and_current_agent_access(client, platform):
    await seed_run(platform, 'succeeded', offset=1, completed=True)
    await seed_run(platform, 'succeeded', offset=2, completed=True)
    await seed_run(platform, 'partial', offset=3, completed=False)
    await seed_run(platform, 'failed', offset=4, completed=False)
    await seed_run(platform, 'cancelled', offset=5, completed=False)
    await seed_run(platform, 'running', offset=6)
    result = await client.get('/v1/stats/summary', params={'start_at': START, 'end_at': END})
    assert result.status_code == 200, result.text
    body = result.json()
    assert body['counts']['runs'] == 6
    assert body['counts']['terminal'] == 5
    assert body['counts']['active'] == 1
    assert body['rates']['success'] == 0.4
    assert body['rates']['partial'] == 0.2


async def test_statistics_rejects_viewer_and_invalid_range(client, platform):
    from uuid import uuid4
    from sqlalchemy import update

    await platform.teams.set_member(
        platform.auth, 'stat-viewer', 'viewer', 'active', expected_revision=0, key=str(uuid4())
    )
    headers = {'Authorization': f'Bearer development:{platform.auth.team_id}:stat-viewer'}
    denied = await client.get(
        '/v1/stats/summary',
        params={'start_at': START, 'end_at': END},
        headers=headers,
    )
    assert denied.status_code == 403
    invalid = await client.get(
        '/v1/stats/summary',
        params={'start_at': 'bad', 'end_at': END},
    )
    assert invalid.status_code == 422
    too_long = await client.get(
        '/v1/stats/summary',
        params={'start_at': '2026-01-01T00:00:00Z', 'end_at': END},
    )
    assert too_long.status_code == 422

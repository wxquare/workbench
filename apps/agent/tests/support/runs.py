"""Public queued-run payload helper and fixture for subsequent worker tasks."""

import pytest


def make_run_payload(agent_id, *, thread_id=None, mode='qa', client_message_id=None):
    return {
        'agent_id': agent_id,
        'thread_id': thread_id,
        'mode': mode,
        'input': {'type': 'text', 'text': 'What happened?', 'artifact_ids': []},
        'client_message_id': client_message_id,
    }


@pytest.fixture
def run_payload(platform):
    return make_run_payload(platform.agent_id, client_message_id='input-1')


async def create_legacy_run(backend, auth, agent_id):
    """Seed pre-T10 admission rows on actual v4/v5 schemas, without new policy.

    Historical-upgrade tests cannot use current RunService.create: admission now
    requires the policy table which deliberately does not exist in those schemas.
    The old rows still use real configuration resolution, stores and transactions.
    """
    from uuid import uuid4
    from workbench_agent.application.context import ContextResolver
    from workbench_agent.application.idempotency import canonical
    from workbench_agent.domain.runs import AcceptedRun, timestamp, run_links

    async with backend.create_uow().begin_write() as uow:
        context = await ContextResolver(backend).resolve(auth, agent_id, uow=uow)
        now = await backend.clock.now_ms(uow)
        thread_id, run_id, message_id = str(uuid4()), str(uuid4()), str(uuid4())
        await backend.runs.insert(
            uow,
            'threads',
            dict(
                team_id=auth.team_id,
                thread_id=thread_id,
                agent_id=agent_id,
                active_run_id=None,
                revision=0,
                created_at=now,
                updated_at=now,
            ),
        )
        await backend.runs.insert(
            uow,
            'runs',
            dict(
                team_id=auth.team_id,
                run_id=run_id,
                thread_id=thread_id,
                agent_id=agent_id,
                status='queued',
                phase=None,
                runtime_profile_id=context.agent_profile.resource_id,
                versions=context.model_dump_json(),
                budgets=canonical(
                    {'wall_seconds': 180, 'model_calls': 8, 'tool_calls': 20, 'tokens': 80000}
                ),
                revision=1,
                last_event_seq=0,
                wall_deadline_at=now + 48 * 3600 * 1000,
                created_at=now,
                updated_at=now,
            ),
        )
        await backend.runs.insert(
            uow,
            'messages',
            dict(
                team_id=auth.team_id,
                message_id=message_id,
                thread_id=thread_id,
                run_id=run_id,
                role='user',
                content_ref=canonical(make_run_payload(agent_id)),
                created_at=now,
                updated_at=now,
            ),
        )
        await backend.runs.insert(
            uow,
            'run_jobs',
            dict(
                team_id=auth.team_id,
                job_id=str(uuid4()),
                run_id=run_id,
                generation=1,
                kind='run',
                status='ready',
                available_at=now,
                capabilities='[]',
                created_at=now,
                updated_at=now,
            ),
        )
        await backend.execution_events.append_control(
            uow, auth.team_id, run_id, 'run.accepted', {'mode': 'qa', 'phase': None, 'attempt': 0}
        )
    return AcceptedRun(
        run_id=run_id, thread_id=thread_id, accepted_at=timestamp(now), links=run_links(run_id)
    )

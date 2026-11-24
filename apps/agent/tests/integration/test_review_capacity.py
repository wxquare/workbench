import json
import time
from uuid import uuid4

import pytest
from sqlalchemy import event, insert, text

from workbench_agent.persistence import schema


@pytest.mark.asyncio
async def test_review_candidates_handle_ten_thousand_and_use_indexes(platform):
    from workbench_agent.domain.identity import AuthContext
    from workbench_agent.application.reviews import ReviewService

    team_id, agent_id = platform.auth.team_id, platform.agent_id
    now = 1_900_000_000_000
    from workbench_agent.persistence.checkpoints.codec import CheckpointCodec
    checkpoint_codec = CheckpointCodec()
    runs = []
    interrupts = []
    checkpoint_interrupts = []
    for index in range(5_000):
        run_id = f'run-{index:05d}'
        thread_id = f'thread-{index:05d}'
        profile_id = f'profile-{index:05d}'
        runs.append({
            'team_id': team_id,
            'run_id': run_id,
            'thread_id': thread_id,
            'agent_id': agent_id,
            'status': 'waiting_input',
            'runtime_profile_id': profile_id,
            'versions': '{}',
            'budgets': '{}',
            'wall_deadline_at': now + 100_000,
            'waiting_deadline_at': now + 50_000,
            'created_at': now - index,
            'updated_at': now - index,
        })
        interrupts.append({
            'team_id': team_id,
            'interrupt_id': f'interrupt-{index:05d}',
            'run_id': run_id,
            'graph_interrupt_id': f'graph-{index:05d}',
            'kind': 'input',
            'status': 'open',
            'schema': json.dumps({'kind': 'user_input', 'actor': platform.auth.subject}),
            'expires_at': now + 60_000,
            'args_hash': None,
            'accepted_resume_request_id': None,
            'revision': 0,
            'created_at': now - index,
            'updated_at': now - index,
        })
        from langgraph.types import Interrupt
        checkpoint_interrupts.append(
            Interrupt(
                id=interrupts[-1]['graph_interrupt_id'],
                value={'kind': 'user_input', 'actor': platform.auth.subject},
            )
        )
    from langgraph.types import Interrupt
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(insert(schema.threads), {
            'team_id': team_id,
            'thread_id': 'capacity-thread',
            'agent_id': agent_id,
            'active_run_id': None,
            'revision': 0,
            'created_at': now,
            'updated_at': now,
        })
        existing_profile = (await uow.execute(
            text(
                'SELECT team_id, profile_id FROM runtime_profiles '
                'WHERE team_id=:team AND agent_id=:agent LIMIT 1'
            ),
            {'team': team_id, 'agent': agent_id},
        )).mappings().first()
        profile_id = existing_profile['profile_id']
        runs = [
            {**run, 'thread_id': 'capacity-thread',
             'runtime_profile_id': profile_id}
            for run in runs
        ]
        await uow.execute(insert(schema.runs), runs)
        await uow.execute(insert(schema.interrupts), interrupts)
        checkpoint = {
            'v': 1,
            'id': 'checkpoint',
            'ts': '2026-11-24T00:00:00Z',
            'channel_values': {
                'run_id': 'run-00000',
                'input_text': 'question',
                'mode': 'qa',
            },
            'channel_versions': {},
            'versions_seen': {},
            'updated_channels': [],
        }
        metadata = {'source': 'loop', 'step': 1, 'parents': {}}
        ptype, payload = checkpoint_codec.dumps_typed(checkpoint)
        mtype, encoded_metadata = checkpoint_codec.dumps_typed(metadata)
        await uow.execute(insert(schema.graph_checkpoints), {
            'team_id': team_id,
            'run_id': 'run-00000',
            'namespace_hash': 'capacity-namespace',
            'namespace': '',
            'checkpoint_id': 'checkpoint',
            'parent_id': None,
            'payload_type': ptype,
            'payload': payload,
            'metadata_type': mtype,
            'metadata': encoded_metadata,
            'codec_version': checkpoint_codec.version,
            'run_epoch': 0,
            'created_at': now,
        })
        wtype, write_payload = checkpoint_codec.dumps_typed(checkpoint_interrupts)
        await uow.execute(insert(schema.graph_pending_writes), {
            'team_id': team_id,
            'run_id': 'run-00000',
            'namespace_hash': 'capacity-namespace',
            'checkpoint_id': 'checkpoint',
            'task_id': 'capacity-task',
            'write_index': 0,
            'channel': '__interrupt__',
            'payload_type': wtype,
            'payload': write_payload,
            'task_path': '',
            'run_epoch': 0,
        })
        other_team = str(uuid4())
        await uow.execute(insert(schema.teams), {
            'team_id': other_team,
            'name': 'other-capacity-team',
            'status': 'active',
            'active_executions': 0,
            'execution_limit': 2,
            'revision': 0,
            'created_at': now,
            'updated_at': now,
        })
        await uow.execute(insert(schema.agents), {
            'team_id': other_team,
            'agent_id': str(uuid4()),
            'name': 'other-capacity-agent',
            'status': 'active',
            'revision': 0,
            'created_at': now,
            'updated_at': now,
        })
        other_agent = (await uow.execute(text(
            'SELECT agent_id FROM agents WHERE team_id=:team'
        ), {'team': other_team})).scalar_one()
        await uow.execute(insert(schema.threads), {
            'team_id': other_team,
            'thread_id': 'other-capacity-thread',
            'agent_id': other_agent,
            'active_run_id': None,
            'revision': 0,
            'created_at': now,
            'updated_at': now,
        })
        await uow.execute(text(
            "INSERT INTO runtime_profiles("
            "team_id,profile_id,agent_id,version,graph_id,profile_json,hash,status,"
            "created_at,updated_at"
            ") SELECT :team,:profile,:agent,1,graph_id,profile_json,hash,status,"
            ":now,:now FROM runtime_profiles "
            "WHERE team_id=:source_team AND profile_id=:source_profile"
        ), {
            'team': other_team,
            'profile': 'other-capacity-profile',
            'agent': other_agent,
            'now': now,
            'source_team': team_id,
            'source_profile': profile_id,
        })
        await uow.execute(insert(schema.runs), [{
            'team_id': other_team,
            'run_id': 'other-run',
            'thread_id': 'other-capacity-thread',
            'agent_id': other_agent,
            'status': 'waiting_input',
            'runtime_profile_id': 'other-capacity-profile',
            'versions': '{}',
            'budgets': '{}',
            'run_epoch': 0,
            'revision': 0,
            'last_event_seq': 0,
            'wall_deadline_at': now + 100_000,
            'waiting_deadline_at': now + 50_000,
            'active_elapsed_ms': 0,
            'event_floor': 0,
            'created_at': now,
            'updated_at': now,
        }])

    started = time.monotonic()
    checkpoint_queries = []
    def count_checkpoint_queries(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith('SELECT') and 'graph_checkpoints' in statement:
            checkpoint_queries.append(statement)

    event.listen(platform.backend.engine.sync_engine, 'before_cursor_execute', count_checkpoint_queries)
    try:
        result = await ReviewService(platform.backend).list(
            platform.auth, state='pending', limit=20
        )
    finally:
        event.remove(platform.backend.engine.sync_engine, 'before_cursor_execute', count_checkpoint_queries)
    elapsed = time.monotonic() - started

    # One fixture carries a real checkpoint; the remaining rows still prove
    # candidate query, capacity protection and index planning.
    assert len(result['items']) == 1
    assert result['items'][0]['run_id'] == 'run-00000'
    assert len(checkpoint_queries) < 100
    assert elapsed < 5

    async with platform.backend.create_uow().begin_read() as uow:
        plan = (await uow.execute(text(
            'EXPLAIN QUERY PLAN '
            'SELECT runs.run_id FROM runs '
            'JOIN interrupts ON interrupts.team_id=runs.team_id '
            'AND interrupts.run_id=runs.run_id '
            'WHERE runs.team_id=:team AND runs.agent_id=:agent '
            'AND runs.status="waiting_input" '
            'AND interrupts.status="open" '
            'AND json_extract(interrupts.schema, "$.actor")=:subject'
        ), {
            'team': team_id,
            'agent': agent_id,
            'subject': platform.auth.subject,
        })).mappings().all()
    details = ' '.join(row['detail'] for row in plan)
    assert (
        'ix_review_runs_team_agent_status_waiting' in details
        or 'ix_review_interrupts_team_status_actor' in details
    ), details

    # Missing checkpoints must still count toward the candidate limit.
    from workbench_agent.application.errors import DomainError
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(insert(schema.runs), {
            **runs[-1], 'run_id': 'run-over-limit',
        })
        await uow.execute(insert(schema.interrupts), {
            **interrupts[-1], 'run_id': 'run-over-limit',
            'interrupt_id': 'interrupt-over-limit',
            'graph_interrupt_id': 'graph-over-limit',
        })
    with pytest.raises(DomainError) as error:
        await ReviewService(platform.backend).list(platform.auth, state='pending', limit=20)
    assert error.value.code == 'REVIEWS_CAPACITY_EXCEEDED'

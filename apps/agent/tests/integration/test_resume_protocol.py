import sqlite3
import pytest
from workbench_agent.cli.db import migrate
from support.interrupts import InterruptPlatform
from support.jobs import claim, row
from workbench_agent.persistence import schema as s
from workbench_agent.jobs.worker import Execution
from test_storage_boundary_migration import upgrade_historical, seed_historical_run, snapshot
from workbench_agent.application.review_facts import run_review_facts
from workbench_agent.application.review_eligibility import review_eligibility


@pytest.mark.asyncio
async def test_resume_protocol_is_submitted_applying_applied_consumed(platform):
    p = InterruptPlatform(platform)
    run, w = await p.waiting_run()
    await p.resume(run, w, key='canonical')
    assert await p.resume_status(run) == 'submitted'
    executor = p.worker.registry._executors['conversation.multi_turn']
    graph = executor.registry.resolve('conversation.multi_turn', 'dod-state-v2')
    original = graph.ainvoke

    async def invoke(*args, **kwargs):
        assert await p.resume_status(run) == 'applying'
        return await original(*args, **kwargs)

    graph.ainvoke = invoke
    owned = await claim(platform)
    await executor.apply_resume(Execution(platform.backend, owned), mark=False)
    assert await p.resume_status(run) == 'applying'
    await executor.apply_resume(Execution(platform.backend, owned))
    assert await p.resume_status(run) == 'applied'
    assert (await row(platform, s.interrupts, interrupt_id=w['interrupt_id']))[
        'status'
    ] == 'consumed'


@pytest.mark.asyncio
async def test_review_projection_matches_live_run_input_eligibility(platform):
    p = InterruptPlatform(platform)
    run, _ = await p.waiting_run()
    async with platform.backend.create_uow().begin_read() as uow:
        run_row = await platform.backend.runs.one(
            uow, 'runs', team_id=platform.auth.team_id, run_id=run.run_id
        )
        interrupt = await platform.backend.runs.waiting(uow, platform.auth.team_id, run.run_id)
        checkpoint = await platform.backend.runs.checkpoint(uow, platform.auth.team_id, run.run_id)
        facts = await run_review_facts(
            platform.backend, uow, platform.auth, run_row, interrupt, checkpoint
        )
    result = review_eligibility('run_input', facts=facts, now_ms=facts['now_ms'])
    assert result.visible is True and result.expired is False
    assert result.operations == ('resume',)


@pytest.mark.asyncio
async def test_review_projection_hides_input_from_a_different_operator(platform):
    from workbench_agent.domain.identity import AuthContext

    p = InterruptPlatform(platform)
    run, _ = await p.waiting_run()
    other = AuthContext(
        team_id=platform.auth.team_id,
        subject='other-operator',
        roles=('operator',),
        identity_kind='user',
    )
    async with platform.backend.create_uow().begin_read() as uow:
        run_row = await platform.backend.runs.one(
            uow, 'runs', team_id=platform.auth.team_id, run_id=run.run_id
        )
        interrupt = await platform.backend.runs.waiting(uow, platform.auth.team_id, run.run_id)
        checkpoint = await platform.backend.runs.checkpoint(uow, platform.auth.team_id, run.run_id)
        facts = await run_review_facts(platform.backend, uow, other, run_row, interrupt, checkpoint)
    result = review_eligibility('run_input', facts=facts, now_ms=facts['now_ms'])
    assert result.visible is False and result.operations == ()


def prepare_v4_resume(config_data, applied=False):
    from workbench_agent.settings import Settings

    path = Settings.model_validate(config_data).storage.database_path
    upgrade_historical(path, '0004_outcomes_events')
    seed_historical_run(path, status='waiting_input')
    with sqlite3.connect(path) as db:
        db.execute('PRAGMA foreign_keys=ON')
        db.execute(
            """INSERT INTO interrupts(team_id,interrupt_id,run_id,graph_interrupt_id,kind,status,schema,expires_at)
            VALUES ('team','interrupt','run','graph-interrupt','input',?,'{}',172801000)""",
            ('applied' if applied else 'response_submitted',),
        )
        db.execute(
            """INSERT INTO resume_requests(team_id,request_id,run_id,interrupt_id,response,status,idempotency_key)
            VALUES ('team','request','run','interrupt','{"text":"answer"}',?,'legacy')""",
            ('applied' if applied else 'accepted',),
        )
        db.execute("UPDATE interrupts SET accepted_resume_request_id='request'")
        db.execute("""INSERT INTO idempotency_records(team_id,record_id,workspace_id,subject,route,key,request_hash,
            response_ref,status_code,expires_at) VALUES ('team','receipt','workspace','user','run.resume','legacy',
            'hash','{"status":"submitted"}',202,172801000)""")
        db.execute("""INSERT INTO run_jobs(team_id,job_id,run_id,generation,kind,status,available_at,capabilities)
            VALUES ('team','job','run',1,'resume','queued',1000,'[]')""")
        db.execute("""INSERT INTO graph_checkpoints(team_id,run_id,namespace_hash,namespace,checkpoint_id,
            payload_type,payload,metadata_type,metadata,codec_version,run_epoch,created_at)
            VALUES ('team','run','namespace-hash','','checkpoint','bytes',X'0080FF','bytes',X'7B7D',
            'dod-checkpoint-v1',0,1000)""")
        db.execute("""INSERT INTO graph_pending_writes(team_id,run_id,namespace_hash,checkpoint_id,task_id,
            write_index,channel,payload_type,payload,task_path,run_epoch)
            VALUES ('team','run','namespace-hash','checkpoint','task',0,'answer','bytes',X'00FF','node',0)""")
    return path


@pytest.mark.parametrize('applied', [False, True])
def test_real_v4_alias_upgrade_preserves_history_and_receipt(config_data, applied):
    path = prepare_v4_resume(config_data, applied)
    with sqlite3.connect(path) as db:
        before = {
            name: db.execute('SELECT * FROM ' + name).fetchall()
            for name in (
                'runs',
                'run_jobs',
                'idempotency_records',
                'graph_checkpoints',
                'graph_pending_writes',
                'runtime_profiles',
            )
        }
    upgrade_historical(path, '0005_resume_protocol')
    upgrade_historical(path, '0005_resume_protocol')
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT version_num FROM alembic_version').fetchone() == (
            '0005_resume_protocol',
        )
        assert db.execute('SELECT status FROM resume_requests').fetchone() == (
            'applied' if applied else 'submitted',
        )
        assert db.execute('SELECT status FROM interrupts').fetchone() == (
            'consumed' if applied else 'response_submitted',
        )
        for name, values in before.items():
            assert db.execute('SELECT * FROM ' + name).fetchall() == values
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    with pytest.raises(RuntimeError, match='fresh.*database'):
        migrate(f'sqlite+aiosqlite:///{path}')


def test_alias_upgrade_refuses_ambiguous_applied_interrupt_atomically(config_data):
    path = prepare_v4_resume(config_data)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE interrupts SET status='applied'")
    before = snapshot(path)
    with pytest.raises(RuntimeError, match='ambiguous legacy resume'):
        upgrade_historical(path, '0005_resume_protocol')
    assert snapshot(path) == before
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT version_num FROM alembic_version').fetchone() == (
            '0004_outcomes_events',
        )
        assert db.execute('SELECT status FROM resume_requests').fetchone() == ('accepted',)


@pytest.mark.asyncio
async def test_old_protocol_revision_is_not_ready_before_explicit_migration(platform):
    with sqlite3.connect(platform.settings.storage.database_path) as db:
        db.execute("UPDATE alembic_version SET version_num='0004_outcomes_events'")
    assert not await platform.backend.health()

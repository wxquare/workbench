"""Historical timing migrations and current Agent graph publication."""

import sqlite3

import pytest

from workbench_agent.cli.bootstrap import BootstrapService
from workbench_agent.cli.db import migrate
from workbench_agent.domain.context import RunContext
from workbench_agent.domain.identity import AuthContext
from workbench_agent.persistence import schema as s
from workbench_agent.persistence.backends.sqlite import SQLiteBackend
from workbench_agent.persistence.schema_v1 import metadata as v1
from workbench_agent.persistence.schema_v2 import metadata as v2
from workbench_agent.persistence.schema_v3 import metadata as v3
from workbench_agent.settings import Settings
from support.interrupts import InterruptPlatform
from support.jobs import row
from test_storage_boundary_migration import seed_historical_run, snapshot, upgrade_historical


@pytest.mark.parametrize('status', [None, 'queued'])
def test_actual_v2_empty_or_never_started_upgrade_has_exact_timing(tmp_path, status):
    path = tmp_path / 'legacy.db'
    upgrade_historical(path, '0002_team_receipts')
    if status is not None:
        seed_historical_run(path, status=status)
    upgrade_historical(path)
    with sqlite3.connect(path) as db:
        assert db.execute(
            'SELECT started_at,active_elapsed_ms,active_since FROM runs'
        ).fetchall() == ([] if status is None else [(None, 0, None)])
        assert db.execute('SELECT version_num FROM alembic_version').fetchone() == (
            '0021_synthetic_batch_policy',
        )
    assert 'started_at' not in v1.tables['runs'].c and 'started_at' not in v2.tables['runs'].c
    assert 'started_at' in v3.tables['runs'].c


@pytest.mark.parametrize(
    'status,epoch', [('waiting_input', 1), ('queued', 2), ('running', 3), ('failed', 4)]
)
def test_actual_v2_executed_legacy_timing_fails_closed_before_ddl(tmp_path, status, epoch):
    path = tmp_path / 'legacy.db'
    upgrade_historical(path, '0002_team_receipts')
    seed_historical_run(path, status=status, epoch=epoch)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE runs SET status='waiting_input',deadline_at=181000,updated_at=6000")
        db.execute("UPDATE runs SET status='queued',deadline_at=NULL,updated_at=90000")
        db.execute('UPDATE runs SET status=?,deadline_at=400000,updated_at=250000', (status,))
    before = snapshot(path)
    with pytest.raises(RuntimeError, match='executed legacy Runs.*offline reconciliation'):
        upgrade_historical(path)
    assert snapshot(path) == before


async def test_bootstrap_preserves_published_graph_and_new_publication_keeps_old_run_pinned(
    platform,
):
    old = await platform.create_run()
    original = await row(platform, s.runs, run_id=old.run_id)
    assert (
        RunContext.model_validate_json(original['versions']).graph_id == 'conversation.multi_turn'
    )
    await platform.bootstrap()
    async with platform.backend.create_uow().begin_read() as uow:
        current = await platform.profiles.published(uow, platform.auth, platform.agent_id)
    assert current.graph_id == 'conversation.multi_turn'
    from workbench_agent.domain.profiles import ProfileDraft

    draft = await platform.profiles.get_agent(platform.auth, platform.agent_id)
    saved = await platform.profiles.save_draft(
        platform.auth,
        platform.agent_id,
        ProfileDraft(default_model=current.default_model, graph_id='conversation.single_turn'),
        draft['revision'],
        key='new-graph-draft',
    )
    await platform.profiles.publish(
        platform.auth, platform.agent_id, saved['revision'], key='new-graph-publish'
    )
    new = await platform.create_run('new')
    assert (await row(platform, s.runs, run_id=old.run_id))['versions'] == original['versions']
    assert (
        RunContext.model_validate_json(
            (await row(platform, s.runs, run_id=new.run_id))['versions']
        ).graph_id
        == 'conversation.single_turn'
    )


async def test_fresh_seed_defaults_to_multiturn_qa(config_data):
    from workbench_agent.application.profiles import ProfileService

    settings = Settings.model_validate(config_data)
    migrate(config_data['storage']['database_url'])
    backend = SQLiteBackend(config_data['storage']['database_url'])
    try:
        await BootstrapService(backend).apply(settings)
        auth = AuthContext(
            team_id=settings.bootstrap.team_id,
            subject=settings.bootstrap.admin_subject,
            roles=('admin',),
        )
        async with backend.create_uow().begin_read() as uow:
            profile = await ProfileService(backend).published(
                uow, auth, settings.bootstrap.agent_id
            )
        assert profile.graph_id == 'conversation.multi_turn'
        assert profile.allowed_modes == ('qa',)
    finally:
        await backend.close()


def test_v3_executed_rows_upgrade_to_outcomes_without_rewriting_history(tmp_path):
    path = tmp_path / 'legacy.db'
    upgrade_historical(path, '0003_run_timing')
    seed_historical_run(path)
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE runs SET status='running',run_epoch=1,started_at=1234,active_elapsed_ms=5678,active_since=9000"
        )
        before = db.execute('SELECT * FROM runs').fetchone()
        db.execute(
            "INSERT INTO run_events(team_id,run_id,seq,type,payload,occurred_at) SELECT team_id,run_id,1,'run.waiting','{}',1234 FROM runs"
        )
        old_event = db.execute('SELECT * FROM run_events').fetchone()
    upgrade_historical(path)
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT * FROM runs').fetchone() == (*before, 0)
        assert db.execute('SELECT * FROM run_events').fetchone() == (*old_event, '1', None)
        assert db.execute('SELECT count(*) FROM run_outcomes').fetchone() == (0,)
    assert 'run_outcomes' not in v3.tables and 'message_id' not in v3.tables['run_events'].c

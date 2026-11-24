"""Raw historical fixtures keep immutable migrations independent of current runtime."""

from pathlib import Path
import sqlite3

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import create_engine

from workbench_agent.cli.db import migrate
from workbench_agent.persistence import migrations


def upgrade_historical(path, revision='head'):
    config = Config()
    config.set_main_option('script_location', str(Path(migrations.__file__).parent))
    engine = create_engine(f'sqlite:///{path}')
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql('PRAGMA foreign_keys=ON')
            connection.exec_driver_sql('PRAGMA journal_mode=WAL')
            connection.commit()
            connection.exec_driver_sql('BEGIN IMMEDIATE')
            config.attributes['connection'] = connection
            try:
                command.upgrade(config, revision)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
    finally:
        engine.dispose()


def seed_historical_run(path, *, status='queued', epoch=0):
    with sqlite3.connect(path) as db:
        db.execute('PRAGMA foreign_keys=ON')
        db.execute("INSERT INTO teams(team_id,name,status) VALUES ('team','Legacy','active')")
        db.execute(
            "INSERT INTO workspaces(team_id,workspace_id,name) VALUES ('team','workspace','Legacy')"
        )
        db.execute("""INSERT INTO runtime_profiles(team_id,profile_id,workspace_id,version,graph_version,
            profile_json,hash,status) VALUES ('team','profile','workspace',1,'dod.v1','{}','hash','published')""")
        db.execute(
            "INSERT INTO threads(team_id,thread_id,workspace_id) VALUES ('team','thread','workspace')"
        )
        db.execute(
            """INSERT INTO runs(team_id,run_id,thread_id,workspace_id,status,runtime_profile_id,
            versions,budgets,run_epoch,wall_deadline_at,created_at,updated_at)
            VALUES ('team','run','thread','workspace',?,'profile','{}','{}',?,172801000,1000,1000)""",
            (status, epoch),
        )


def snapshot(path):
    with sqlite3.connect(path) as db:
        return '\n'.join(db.iterdump())


@pytest.mark.parametrize(
    'revision',
    [
        '0001_core',
        '0002_team_receipts',
        '0005_resume_protocol',
        '0012_schedule_manual_fires',
        'head',
    ],
)
@pytest.mark.parametrize('populated', [False, True])
def test_every_legacy_baseline_is_rejected_untouched(tmp_path, revision, populated):
    path = tmp_path / 'legacy.db'
    upgrade_historical(path, revision)
    if populated:
        seed_historical_run(path)
    before = snapshot(path)
    files_before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    with pytest.raises(RuntimeError, match='fresh.*database'):
        migrate(f'sqlite+aiosqlite:///{path}')
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir()} == files_before
    assert snapshot(path) == before

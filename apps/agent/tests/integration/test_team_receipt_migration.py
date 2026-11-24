"""Exercise actual 0001->0002 maintenance, old receipts and failure rollback."""

import sqlite3
from pathlib import Path
from uuid import UUID
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine
from workbench_agent.application.idempotency import canonical, digest
from workbench_agent.cli.doctor import check_sqlite_runtime
from workbench_agent.persistence.backends.sqlite import SQLiteBackend
from workbench_agent.persistence.schema_v1 import metadata as v1_metadata
from workbench_agent.settings import Settings
from test_storage_boundary_migration import upgrade_historical

TEAM = str(UUID(int=500))
WS = str(UUID(int=501))
OTHER_WS = str(UUID(int=502))
ORIGINAL_RESPONSE = {
    'team_id': str(UUID(int=600)),
    'name': 'original',
    'revision': 0,
    'workspace_id': str(UUID(int=601)),
}


def prepare_v1(config_data):
    from workbench_agent.persistence import migrations

    config = Config()
    config.set_main_option('script_location', str(Path(migrations.__file__).parent))
    engine = create_engine(
        config_data['storage']['database_url'].replace('sqlite+aiosqlite:', 'sqlite:')
    )
    with engine.connect() as connection:
        connection.exec_driver_sql('PRAGMA foreign_keys=ON')
        connection.exec_driver_sql('PRAGMA journal_mode=WAL')
        connection.exec_driver_sql('PRAGMA synchronous=FULL')
        connection.commit()
        connection.exec_driver_sql('BEGIN IMMEDIATE')
        config.attributes['connection'] = connection
        command.upgrade(config, '0001_core')
        connection.commit()
    engine.dispose()
    path = Settings.model_validate(config_data).storage.database_path
    with sqlite3.connect(path) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert tables == set(v1_metadata.tables) | {'alembic_version'}
        assert 'team_idempotency_records' not in v1_metadata.tables
        db.execute('PRAGMA foreign_keys=ON')
        db.execute(
            'INSERT INTO teams(team_id,name,status) VALUES (?,?,?)', (TEAM, 'legacy', 'active')
        )
        db.execute(
            'INSERT INTO team_members(team_id,member_id,subject,role,status) VALUES (?,?,?,?,?)',
            (TEAM, str(UUID(int=503)), 'legacy-admin', 'admin', 'active'),
        )
        for workspace_id in (WS, OTHER_WS):
            db.execute(
                'INSERT INTO workspaces(team_id,workspace_id,name) VALUES (?,?,?)',
                (TEAM, workspace_id, workspace_id),
            )
    return path


def add_receipt(
    db,
    record_id,
    workspace_id,
    *,
    route='team.create',
    key='same',
    payload=None,
    response=None,
    expires_at=None,
    created_at=None,
):
    now = db.execute("SELECT CAST((julianday('now')-2440587.5)*86400000 AS INTEGER)").fetchone()[0]
    payload = {'name': 'original'} if payload is None else payload
    response = ORIGINAL_RESPONSE if response is None else response
    db.execute(
        """INSERT INTO idempotency_records(team_id,record_id,workspace_id,subject,route,key,request_hash,
        response_ref,status_code,expires_at,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            TEAM,
            str(UUID(int=record_id)),
            workspace_id,
            'legacy-admin',
            route,
            key,
            digest(payload),
            canonical(response),
            201,
            expires_at if expires_at is not None else now + 259200000,
            created_at if created_at is not None else now,
            now,
        ),
    )


async def test_v1_upgrade_preserves_workspace_receipts_and_copies_team_receipt(config_data):
    path = prepare_v1(config_data)
    with sqlite3.connect(path) as db:
        add_receipt(db, 700, WS)
        add_receipt(
            db,
            701,
            WS,
            route='workspace.draft',
            key='workspace',
            payload={'draft': 'unchanged'},
            response={'revision': 1},
        )
        before = db.execute('SELECT * FROM idempotency_records ORDER BY record_id').fetchall()
    old_backend = SQLiteBackend(config_data['storage']['database_url'])
    assert await old_backend.health() is False
    assert check_sqlite_runtime(Settings.model_validate(config_data)).ready is False
    await old_backend.close()
    upgrade_historical(path)
    upgrade_historical(path)
    with sqlite3.connect(path) as db:
        assert (
            db.execute('SELECT * FROM idempotency_records ORDER BY record_id').fetchall() == before
        )
        assert db.execute('SELECT response_ref FROM team_idempotency_records').fetchall() == [
            (canonical(ORIGINAL_RESPONSE),)
        ]
        assert db.execute('SELECT count(*) FROM teams').fetchone()[0] == 1
    # Even a fully upgraded historical database is outside the Agent boundary.
    assert not check_sqlite_runtime(Settings.model_validate(config_data)).ready


@pytest.mark.parametrize('conflict', ['response', 'request'])
def test_ambiguous_active_legacy_receipts_abort_migration_without_merging_teams(
    config_data, conflict
):
    path = prepare_v1(config_data)
    with sqlite3.connect(path) as db:
        add_receipt(db, 700, WS)
        if conflict == 'response':
            add_receipt(
                db, 701, OTHER_WS, response={**ORIGINAL_RESPONSE, 'team_id': str(UUID(int=999))}
            )
        else:
            add_receipt(db, 701, OTHER_WS, payload={'name': 'different-request'})
        before = db.execute('SELECT * FROM idempotency_records ORDER BY record_id').fetchall()
    with pytest.raises(RuntimeError, match='ambiguous active team command receipts'):
        upgrade_historical(path)
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT version_num FROM alembic_version').fetchone()[0] == '0001_core'
        assert (
            db.execute(
                "SELECT name FROM sqlite_master WHERE name='team_idempotency_records'"
            ).fetchall()
            == []
        )
        assert (
            db.execute('SELECT * FROM idempotency_records ORDER BY record_id').fetchall() == before
        )
        assert db.execute('SELECT count(*) FROM teams').fetchone()[0] == 1


def test_migration_prefers_live_reuse_window_over_expired_first_receipt(config_data):
    path = prepare_v1(config_data)
    with sqlite3.connect(path) as db:
        now = db.execute(
            "SELECT CAST((julianday('now')-2440587.5)*86400000 AS INTEGER)"
        ).fetchone()[0]
        add_receipt(
            db, 700, WS, payload={'name': 'expired'}, expires_at=now - 1, created_at=now - 259200001
        )
        add_receipt(db, 701, OTHER_WS, created_at=now, expires_at=now + 259200000)
    upgrade_historical(path)
    with sqlite3.connect(path) as db:
        assert db.execute(
            'SELECT record_id,response_ref,created_at,expires_at FROM team_idempotency_records'
        ).fetchall() == [(str(UUID(int=701)), canonical(ORIGINAL_RESPONSE), now, now + 259200000)]

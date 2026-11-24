"""Agent storage has a distinct, non-destructive database boundary."""

import sqlite3
from contextlib import closing

import pytest

from workbench_agent.cli.db import migrate
from workbench_agent.persistence import schema
from workbench_agent.persistence.schema_v20 import metadata as historical
from workbench_agent.persistence.sqlite_diagnostics import validate_required_schema
from workbench_agent.persistence.schema_agent_v3 import metadata as agent_v3


@pytest.mark.parametrize('legacy', [False, True])
def test_migration_cli_reports_safe_initialization_guidance(
    config_data, tmp_path, monkeypatch, capsys, legacy
):
    import json
    import sys
    import yaml
    from workbench_agent.cli.main import main
    from workbench_agent.settings import Settings

    settings = Settings.model_validate(config_data)
    path = settings.storage.database_path
    if legacy:
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE workspaces(value TEXT)')
        before = path.read_bytes()
    else:

        def unavailable(*args):
            raise RuntimeError('private-database-path-and-credential')

        monkeypatch.setattr('workbench_agent.cli.db.migrate', unavailable)
    config = tmp_path / 'migration.yaml'
    config.write_text(yaml.safe_dump(config_data))
    monkeypatch.setattr(sys, 'argv', ['workbench', 'db', 'migrate', '--config', str(config)])
    with pytest.raises(SystemExit) as error:
        main()
    assert error.value.code == 1
    report = json.loads(capsys.readouterr().out)
    assert report['ready'] is False
    assert 'private-database-path-and-credential' not in str(report)
    assert str(path) not in str(report)
    if legacy:
        assert 'fresh Agent database path' in report['errors'][0]
        assert 'bootstrap' in report['errors'][0]
        assert path.read_bytes() == before
    else:
        assert report['errors'] == ['storage maintenance migration failed']


def test_agent_metadata_preserves_historical_structure():
    def mapped(name):
        return name.replace('workspaces', 'agents').replace('workspace_id', 'agent_id')

    assert set(agent_v3.tables) == {mapped(name) for name in historical.tables} | {
        'skill_drafts',
        'run_trace_contexts',
    }
    assert set(schema.metadata.tables) == set(agent_v3.tables) | {'kb_entries', 'kb_entry_versions'}
    for old in historical.tables.values():
        new = agent_v3.tables[mapped(old.name)]
        assert [
            (
                c.name,
                str(c.type),
                c.nullable,
                c.primary_key,
                str(c.server_default.arg) if c.server_default is not None else None,
            )
            for c in new.columns
        ] == [
            (
                mapped(c.name),
                str(c.type),
                c.nullable,
                c.primary_key,
                str(c.server_default.arg) if c.server_default is not None else None,
            )
            for c in old.columns
        ]

        def constraints(table, rename):
            return {
                (
                    type(c).__name__,
                    rename(c.name) if c.name else None,
                    tuple(rename(col.name) for col in c.columns),
                    tuple(rename(e.target_fullname) for e in c.elements)
                    if hasattr(c, 'elements')
                    else (),
                    getattr(c, 'ondelete', None),
                    str(c.sqltext) if hasattr(c, 'sqltext') else None,
                )
                for c in table.constraints
            }

        assert constraints(new, str) == constraints(old, mapped)
        assert {(i.name, i.unique, tuple(c.name for c in i.columns)) for i in new.indexes} == {
            (mapped(i.name), i.unique, tuple(mapped(c.name) for c in i.columns))
            for i in old.indexes
        }
    assert 'workspace_id' in historical.tables['runs'].c


def test_fresh_and_repeated_agent_initialization(tmp_path):
    path = tmp_path / 'agent.db'
    url = f'sqlite+aiosqlite:///{path}'
    migrate(url)
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT * FROM dod_storage_metadata').fetchall() == [
            ('dod-storage-agent-v1', 'dod-checkpoint-v1')
        ]
        assert db.execute('SELECT version_num FROM alembic_version').fetchall() == [
            ('0007_review_indexes',)
        ]
        assert not validate_required_schema(db)
        db.execute("INSERT INTO teams(team_id,name,status) VALUES ('a','a','active')")
        db.execute("INSERT INTO agents(team_id,agent_id,name) VALUES ('a','agent','Agent')")
    migrate(url)
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT agent_id FROM agents').fetchall() == [('agent',)]


def test_upgrade_existing_agent_database_preserves_agents_and_private_skills(tmp_path):
    from sqlalchemy import create_engine
    from workbench_agent.persistence.schema_agent_v1 import metadata

    path = tmp_path / 'old-agent.db'
    engine = create_engine(f'sqlite:///{path}')
    with engine.begin() as db:
        db.exec_driver_sql('PRAGMA journal_mode=WAL')
        metadata.create_all(db)
        db.exec_driver_sql(
            'CREATE TABLE alembic_version(version_num VARCHAR(32) NOT NULL PRIMARY KEY)'
        )
        db.exec_driver_sql("INSERT INTO alembic_version VALUES ('0001_agent_domain')")
        db.exec_driver_sql(
            "INSERT INTO dod_storage_metadata VALUES ('dod-storage-agent-v1','dod-checkpoint-v1')"
        )
        db.exec_driver_sql("INSERT INTO teams(team_id,name,status) VALUES ('t','team','active')")
        db.exec_driver_sql("INSERT INTO agents(team_id,agent_id,name) VALUES ('t','a','Agent')")
        db.exec_driver_sql(
            "INSERT INTO skills(team_id,skill_id,agent_id,name,status) VALUES ('t','s','a','private','active')"
        )
    engine.dispose()
    migrate(f'sqlite+aiosqlite:///{path}')
    with closing(sqlite3.connect(path)) as db:
        assert not validate_required_schema(db)
        assert db.execute('SELECT agent_id FROM agents').fetchall() == [('a',)]
        assert db.execute('SELECT agent_id,name FROM skills').fetchall() == [('a', 'private')]
        assert db.execute('SELECT version_num FROM alembic_version').fetchall() == [
            ('0007_review_indexes',)
        ]


def test_upgrade_shared_skills_database_keeps_drafts(tmp_path):
    from sqlalchemy import create_engine
    from workbench_agent.persistence.schema_agent_v2 import metadata

    path = tmp_path / 'shared-skills.db'
    engine = create_engine(f'sqlite:///{path}')
    with engine.begin() as db:
        db.exec_driver_sql('PRAGMA journal_mode=WAL')
        metadata.create_all(db)
        db.exec_driver_sql(
            'CREATE TABLE alembic_version(version_num VARCHAR(32) NOT NULL PRIMARY KEY)'
        )
        db.exec_driver_sql("INSERT INTO alembic_version VALUES ('0002_shared_skills')")
        db.exec_driver_sql(
            "INSERT INTO dod_storage_metadata VALUES ('dod-storage-agent-v1','dod-checkpoint-v1')"
        )
        db.exec_driver_sql("INSERT INTO teams(team_id,name,status) VALUES ('t','team','active')")
        db.exec_driver_sql(
            "INSERT INTO skills(team_id,skill_id,name,status) VALUES ('t','s','shared','active')"
        )
        db.exec_driver_sql(
            "INSERT INTO skill_drafts VALUES ('s','t','shared','description','keep me',1,1)"
        )
    engine.dispose()
    migrate(f'sqlite+aiosqlite:///{path}')
    with closing(sqlite3.connect(path)) as db:
        assert __import__('json').loads(
            db.execute('SELECT files_json FROM skill_drafts').fetchone()[0]
        ) == {'SKILL.md': {'encoding': 'utf-8', 'content': 'keep me'}}
        assert db.execute('SELECT version_num FROM alembic_version').fetchall() == [
            ('0007_review_indexes',)
        ]
        assert not validate_required_schema(db)


@pytest.mark.parametrize('populated', [False, True])
@pytest.mark.parametrize('stamped', [False, True])
def test_legacy_database_rejected_without_mutation(tmp_path, populated, stamped):
    path = tmp_path / 'legacy.db'
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE workspaces(workspace_id TEXT PRIMARY KEY)')
        if populated:
            db.execute("INSERT INTO workspaces VALUES ('keep-me')")
        if stamped:
            db.execute(
                'CREATE TABLE dod_storage_metadata(schema_version TEXT, checkpoint_codec_version TEXT)'
            )
            db.execute(
                "INSERT INTO dod_storage_metadata VALUES ('dod-storage-v1','dod-checkpoint-v1')"
            )
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match='fresh.*database'):
        migrate(f'sqlite+aiosqlite:///{path}')
    assert path.read_bytes() == before
    assert not path.with_name(path.name + '-wal').exists()
    with sqlite3.connect(path) as db:
        assert validate_required_schema(db)
        assert db.execute('PRAGMA journal_mode').fetchone() == ('delete',)


async def test_agent_health_and_legacy_readiness(tmp_path):
    from workbench_agent.persistence.backends.sqlite import SQLiteBackend

    path = tmp_path / 'agent.db'
    url = f'sqlite+aiosqlite:///{path}'
    migrate(url)
    backend = SQLiteBackend(url)
    try:
        assert await backend.health()
    finally:
        await backend.close()
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE workspaces(workspace_id TEXT)')
    backend = SQLiteBackend(url)
    try:
        assert not await backend.health()
    finally:
        await backend.close()


async def test_agent_bootstrap_is_repeatable(config_data):
    from workbench_agent.cli.bootstrap import BootstrapService
    from workbench_agent.persistence.backends.sqlite import SQLiteBackend
    from workbench_agent.settings import Settings

    settings = Settings.model_validate(config_data)
    url = config_data['storage']['database_url']
    migrate(url)
    backend = SQLiteBackend(url)
    try:
        service = BootstrapService(backend)
        await service.apply(settings)
        with sqlite3.connect(settings.storage.database_path) as db:
            before = db.execute('SELECT * FROM agents').fetchall()
            profiles = db.execute('SELECT * FROM runtime_profiles').fetchall()
        await service.apply(settings)
        migrate(url)
        assert await backend.health()
        with sqlite3.connect(settings.storage.database_path) as db:
            assert db.execute('SELECT * FROM agents').fetchall() == before
            assert db.execute('SELECT * FROM runtime_profiles').fetchall() == profiles
    finally:
        await backend.close()


@pytest.mark.parametrize('table', ['unrelated', 'sqlitexother'])
def test_unknown_database_is_rejected_without_mutation(tmp_path, table):
    path = tmp_path / 'other.db'
    with sqlite3.connect(path) as db:
        db.execute(f'CREATE TABLE {table}(value TEXT)')
        db.execute(f"INSERT INTO {table} VALUES ('retain')")
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match='fresh.*database'):
        migrate(f'sqlite+aiosqlite:///{path}')
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    'replacement',
    [
        None,
        'active_executions >= -100',
        "'CHECK (active_executions >= 0)' IS NOT NULL",
        'active_executions >= -100 /* CHECK (active_executions >= 0) */',
    ],
)
async def test_missing_or_weakened_check_is_unready_and_migration_rejects(tmp_path, replacement):
    from sqlalchemy import CheckConstraint, MetaData, create_engine
    from workbench_agent.persistence.backends.sqlite import SQLiteBackend
    from workbench_agent.persistence.schema_agent_v1 import CHECKPOINT_CODEC_VERSION, SCHEMA_VERSION
    from workbench_agent.persistence.schema_agent_v7 import MIGRATION_HEAD

    path = tmp_path / 'invalid-check.db'
    url = f'sqlite+aiosqlite:///{path}'
    altered = MetaData(naming_convention=dict(schema.metadata.naming_convention))
    for table in schema.metadata.tables.values():
        table.to_metadata(altered)
    teams = altered.tables['teams']
    for constraint in list(teams.constraints):
        if isinstance(constraint, CheckConstraint):
            teams.constraints.remove(constraint)
    if replacement is not None:
        teams.append_constraint(CheckConstraint(replacement, name='weakened_execution_guard'))
    engine = create_engine(f'sqlite:///{path}')
    try:
        with engine.begin() as db:
            db.exec_driver_sql('PRAGMA journal_mode=WAL')
            altered.create_all(db)
            db.exec_driver_sql(
                'CREATE TABLE alembic_version(version_num VARCHAR(32) NOT NULL PRIMARY KEY)'
            )
            db.exec_driver_sql('INSERT INTO alembic_version VALUES (?)', (MIGRATION_HEAD,))
            db.exec_driver_sql(
                'INSERT INTO dod_storage_metadata VALUES (?,?)',
                (SCHEMA_VERSION, CHECKPOINT_CODEC_VERSION),
            )
            db.exec_driver_sql(
                "INSERT INTO teams(team_id,name,status,active_executions) VALUES ('t','t','active',-1)"
            )
    finally:
        engine.dispose()
    with closing(sqlite3.connect(path)) as db:
        assert 'CHECK constraints' in ' '.join(validate_required_schema(db))
    backend = SQLiteBackend(url)
    try:
        assert not await backend.health()
    finally:
        await backend.close()
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match='fresh.*database'):
        migrate(url)
    assert path.read_bytes() == before


def test_non_wal_initialized_database_is_rejected_without_mutation(tmp_path):
    path = tmp_path / 'non-wal.db'
    url = f'sqlite+aiosqlite:///{path}'
    migrate(url)
    with closing(sqlite3.connect(path)) as db:
        assert db.execute('PRAGMA journal_mode=DELETE').fetchone() == ('delete',)
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match='WAL|journal_mode'):
        migrate(url)
    assert path.read_bytes() == before
    assert not path.with_name(path.name + '-wal').exists()


def test_competing_initializer_cannot_write_between_preflight_and_wal(tmp_path, monkeypatch):
    from sqlalchemy import create_engine, event
    import workbench_agent.cli.db as maintenance

    path = tmp_path / 'race.db'
    blocked = []

    def before_statement(connection, cursor, statement, parameters, context, executemany):
        if statement == 'PRAGMA journal_mode=WAL':
            with closing(sqlite3.connect(path, timeout=0)) as rival:
                try:
                    rival.execute('CREATE TABLE workspaces(workspace_id TEXT)')
                    rival.commit()
                except sqlite3.OperationalError as error:
                    assert error.sqlite_errorcode & 255 == sqlite3.SQLITE_BUSY
                    blocked.append(True)

    def observed_engine(*args, **kwargs):
        engine = create_engine(*args, **kwargs)
        event.listen(engine, 'before_cursor_execute', before_statement)
        return engine

    monkeypatch.setattr(maintenance, 'create_engine', observed_engine)
    migrate(f'sqlite+aiosqlite:///{path}')
    assert blocked == [True]
    with closing(sqlite3.connect(path)) as db:
        assert not validate_required_schema(db)
        assert db.execute("SELECT name FROM sqlite_master WHERE name='workspaces'").fetchall() == []


def test_legacy_schema_appearing_before_lock_is_rejected_untouched(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    import workbench_agent.cli.db as maintenance

    path = tmp_path / 'appearing-legacy.db'
    before = []

    def competing_engine(*args, **kwargs):
        with closing(sqlite3.connect(path)) as rival:
            rival.execute('CREATE TABLE workspaces(workspace_id TEXT)')
            rival.execute("INSERT INTO workspaces VALUES ('retain')")
            rival.commit()
        before.append(path.read_bytes())
        return create_engine(*args, **kwargs)

    monkeypatch.setattr(maintenance, 'create_engine', competing_engine)
    with pytest.raises(RuntimeError, match='fresh.*database'):
        migrate(f'sqlite+aiosqlite:///{path}')
    assert path.read_bytes() == before[0]
    assert not path.with_name(path.name + '-wal').exists()
    with closing(sqlite3.connect(path)) as db:
        assert db.execute('PRAGMA journal_mode').fetchone() == ('delete',)
        assert db.execute('SELECT * FROM workspaces').fetchall() == [('retain',)]

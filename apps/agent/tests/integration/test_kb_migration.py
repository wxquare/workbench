import sqlite3

from workbench_agent.cli.db import migrate
from workbench_agent.persistence.sqlite_diagnostics import validate_required_schema


def test_fresh_knowledge_schema_and_missing_index_readiness(config_data):
    url = config_data['storage']['database_url']
    migrate(url)
    path = url.removeprefix('sqlite+aiosqlite:///')
    with sqlite3.connect(path) as db:
        db.execute('PRAGMA foreign_keys=ON')
        assert db.execute('SELECT version_num FROM alembic_version').fetchone() == ('0004_knowledge_base',)
        sql = db.execute("SELECT sql FROM sqlite_master WHERE name='kb_fts'").fetchone()[0]
        assert 'trigram' in sql
        assert validate_required_schema(db) == []
        db.execute('DROP TABLE kb_fts')
        assert validate_required_schema(db)


def test_published_pointer_cannot_reference_another_team_version(config_data):
    url = config_data['storage']['database_url']
    migrate(url)
    path = url.removeprefix('sqlite+aiosqlite:///')
    with sqlite3.connect(path) as db:
        db.execute('PRAGMA foreign_keys=ON')
        definitions = db.execute('PRAGMA foreign_key_list(kb_entries)').fetchall()
        assert any(row[2] == 'kb_entry_versions' for row in definitions)
        assert [row[3] for row in definitions if row[2] == 'kb_entry_versions'] == [
            'team_id', 'entry_id', 'published_version']


async def test_v3_upgrade_preserves_real_run_and_existing_gap(config_data):
    from contextlib import closing
    from pathlib import Path
    from sqlalchemy import create_engine
    from workbench_agent.application.runs import RunService
    from workbench_agent.cli.bootstrap import BootstrapService
    from workbench_agent.domain.identity import AuthContext
    from workbench_agent.domain.runs import CreateRun
    from workbench_agent.persistence.backends.sqlite import SQLiteBackend
    from workbench_agent.persistence.schema_agent_v3 import metadata
    from workbench_agent.settings import Settings

    settings = Settings.model_validate(config_data)
    path = Path(settings.storage.database_path)
    engine = create_engine(f'sqlite:///{path}')
    with engine.begin() as connection:
        connection.exec_driver_sql('PRAGMA journal_mode=WAL')
        metadata.create_all(connection)
        connection.exec_driver_sql('CREATE TABLE alembic_version(version_num VARCHAR(32) NOT NULL PRIMARY KEY)')
        connection.exec_driver_sql("INSERT INTO alembic_version VALUES ('0003_run_trace_contexts')")
        connection.exec_driver_sql("INSERT INTO dod_storage_metadata VALUES ('dod-storage-agent-v1','dod-checkpoint-v1')")
    engine.dispose()
    backend = SQLiteBackend(config_data['storage']['database_url'])
    try:
        await BootstrapService(backend).apply(settings)
        auth = AuthContext(team_id=settings.bootstrap.team_id, subject=settings.bootstrap.admin_subject, roles=('admin',))
        accepted = await RunService(backend).create(auth, CreateRun.model_validate({'agent_id': settings.bootstrap.agent_id,
            'mode': 'qa', 'input': {'text': '问题'}}), 'before-upgrade')
    finally:
        await backend.close()
    with closing(sqlite3.connect(path)) as db, db:
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('''INSERT INTO knowledge_gaps(team_id,gap_id,run_id,source_trace,problem_type,review_status,summary,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?)''', (settings.bootstrap.team_id, 'old-gap', accepted.run_id, '{}', 'old', 'open', 'preserve', 1, 1))
    migrate(config_data['storage']['database_url'])
    with closing(sqlite3.connect(path)) as db:
        assert not validate_required_schema(db)
        assert db.execute('SELECT summary,gap_revision FROM knowledge_gaps WHERE gap_id=?', ('old-gap',)).fetchone() == ('preserve', 0)
        assert db.execute('SELECT run_id FROM runs WHERE run_id=?', (accepted.run_id,)).fetchone() == (accepted.run_id,)

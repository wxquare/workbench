import sqlite3

from workbench_agent.cli.db import migrate
from workbench_agent.persistence.sqlite_diagnostics import validate_required_schema


def test_fresh_knowledge_schema_and_missing_index_readiness(config_data):
    url = config_data['storage']['database_url']
    migrate(url)
    path = url.removeprefix('sqlite+aiosqlite:///')
    with sqlite3.connect(path) as db:
        db.execute('PRAGMA foreign_keys=ON')
        assert db.execute('SELECT version_num FROM alembic_version').fetchone() == ('0006_multifile_skill_drafts',)
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


def test_populated_v3_upgrade_rejects_without_changing_run_or_gap(config_data):
    from pathlib import Path
    import pytest
    from sqlalchemy import create_engine
    from workbench_agent.cli.db import FreshAgentDatabaseRequired
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
        connection.exec_driver_sql("INSERT INTO teams(team_id,name,status) VALUES ('team','Team','active')")
        connection.exec_driver_sql("INSERT INTO agents(team_id,agent_id,name) VALUES ('team','agent','Agent')")
        connection.exec_driver_sql("INSERT INTO runtime_profiles(team_id,profile_id,agent_id,version,graph_version,profile_json,hash,status) VALUES ('team','profile','agent',1,'dod.v18','{}','hash','published')")
        connection.exec_driver_sql("INSERT INTO threads(team_id,thread_id,agent_id) VALUES ('team','thread','agent')")
        connection.exec_driver_sql("INSERT INTO runs(team_id,run_id,thread_id,agent_id,status,runtime_profile_id,versions,budgets,wall_deadline_at) VALUES ('team','run','thread','agent','partial','profile','{}','{}',1000)")
        connection.exec_driver_sql("INSERT INTO knowledge_gaps(team_id,gap_id,run_id,source_trace,problem_type,review_status,summary) VALUES ('team','gap','run','{}','old','open','preserve')")
    engine.dispose()
    with pytest.raises(FreshAgentDatabaseRequired, match='empty database'):
        migrate(config_data['storage']['database_url'])
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT version_num FROM alembic_version').fetchone() == ('0003_run_trace_contexts',)
        assert db.execute('SELECT graph_version FROM runtime_profiles').fetchone() == ('dod.v18',)
        assert db.execute('SELECT run_id FROM runs').fetchone() == ('run',)
        assert db.execute('SELECT summary FROM knowledge_gaps').fetchone() == ('preserve',)

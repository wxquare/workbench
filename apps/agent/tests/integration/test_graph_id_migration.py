"""A fresh database uses the graph_id schema."""
import sqlite3
from workbench_agent.cli.db import migrate


def test_fresh_database_uses_graph_id(config_data):
    url = config_data['storage']['database_url']
    migrate(url)
    with sqlite3.connect(url.removeprefix('sqlite+aiosqlite:///')) as db:
        assert db.execute('SELECT version_num FROM alembic_version').fetchone() == ('0006_multifile_skill_drafts',)
        columns = {row[1] for row in db.execute('PRAGMA table_info(runtime_profiles)')}
        assert 'graph_id' in columns
        assert 'graph_version' not in columns


def test_empty_v4_database_upgrades_without_losing_foreign_keys(config_data):
    from pathlib import Path
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine

    url = config_data['storage']['database_url']
    path = url.removeprefix('sqlite+aiosqlite:///')
    engine = create_engine('sqlite:///' + path)
    config = Config()
    config.set_main_option('script_location', str(Path(__file__).resolve().parents[2] / 'src/workbench_agent/persistence/agent_migrations'))
    with engine.connect() as connection:
        connection.exec_driver_sql('PRAGMA journal_mode=WAL')
        config.attributes['connection'] = connection
        command.upgrade(config, '0004_knowledge_base')
        connection.commit()
    engine.dispose()
    migrate(url)
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT version_num FROM alembic_version').fetchone() == ('0006_multifile_skill_drafts',)
        assert 'graph_id' in {row[1] for row in db.execute('PRAGMA table_info(runtime_profiles)')}
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []


def test_populated_old_database_is_rejected_without_modification(config_data):
    from pathlib import Path
    import pytest
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine
    from workbench_agent.cli.db import FreshAgentDatabaseRequired

    url = config_data['storage']['database_url']
    path = url.removeprefix('sqlite+aiosqlite:///')
    engine = create_engine('sqlite:///' + path)
    config = Config()
    config.set_main_option('script_location', str(Path(__file__).resolve().parents[2] / 'src/workbench_agent/persistence/agent_migrations'))
    with engine.connect() as connection:
        connection.exec_driver_sql('PRAGMA journal_mode=WAL')
        config.attributes['connection'] = connection
        command.upgrade(config, '0004_knowledge_base')
        connection.exec_driver_sql("INSERT INTO teams(team_id,name,status) VALUES ('team','Team','active')")
        connection.exec_driver_sql("INSERT INTO agents(team_id,agent_id,name) VALUES ('team','agent','Agent')")
        connection.exec_driver_sql("INSERT INTO runtime_profiles(team_id,profile_id,agent_id,version,graph_version,profile_json,hash,status) VALUES ('team','profile','agent',1,'dod.v18','{}','hash','published')")
        connection.commit()
    engine.dispose()
    with pytest.raises(FreshAgentDatabaseRequired, match='empty database'):
        migrate(url)
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT version_num FROM alembic_version').fetchone() == ('0004_knowledge_base',)
        assert db.execute('SELECT graph_version FROM runtime_profiles').fetchone() == ('dod.v18',)

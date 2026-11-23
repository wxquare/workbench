"""Upgrading populated v5 must preserve bodies and all existing identities."""
import json
import sqlite3

from sqlalchemy import create_engine

from workbench_agent.cli.db import migrate
from workbench_agent.persistence.schema_agent_v5 import metadata
from workbench_agent.persistence.sqlite_diagnostics import validate_required_schema


def test_populated_v5_preserves_draft_and_published_records(tmp_path):
    path = tmp_path / 'old.db'
    engine = create_engine('sqlite:///' + str(path))
    body = '中文正文\n{"SKILL.md": "这仍是正文"}\n'
    with engine.begin() as db:
        db.exec_driver_sql('PRAGMA journal_mode=WAL')
        metadata.create_all(db)
        # The virtual index is created by the v4 migration rather than metadata.
        db.exec_driver_sql("CREATE VIRTUAL TABLE kb_fts USING fts5(title,content,tags,aliases,team_id UNINDEXED,entry_id UNINDEXED,version UNINDEXED,chunk_id UNINDEXED,heading_path UNINDEXED,line_start UNINDEXED,line_end UNINDEXED,content_hash UNINDEXED,chunker_version UNINDEXED,tokenize='trigram')")
        db.exec_driver_sql('CREATE TABLE alembic_version(version_num VARCHAR(32) NOT NULL PRIMARY KEY)')
        db.exec_driver_sql("INSERT INTO alembic_version VALUES ('0005_graph_id')")
        db.exec_driver_sql("INSERT INTO dod_storage_metadata VALUES ('dod-storage-agent-v1','dod-checkpoint-v1')")
        db.exec_driver_sql("INSERT INTO teams(team_id,name,status) VALUES ('t','team','active')")
        db.exec_driver_sql("INSERT INTO agents(team_id,agent_id,name) VALUES ('t','a','Agent')")
        db.exec_driver_sql("INSERT INTO runtime_profiles(team_id,profile_id,agent_id,version,graph_id,profile_json,hash,status) VALUES ('t','p','a',1,'conversation.multi_turn','{}','hash','published')")
        db.exec_driver_sql("INSERT INTO skills(team_id,skill_id,name,status,revision) VALUES ('t','s','shared','active',8)")
        db.exec_driver_sql("INSERT INTO skill_drafts VALUES ('s','t','shared','description',?,11,12)", (body,))
        db.exec_driver_sql("INSERT INTO skill_versions(team_id,skill_version_id,skill_id,version,hash,bundle_ref,status) VALUES ('t','sv','s',1,'hash','old-ref','published')")
    engine.dispose()
    migrate('sqlite+aiosqlite:///' + str(path))
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT version_num FROM alembic_version').fetchone() == ('0006_multifile_skill_drafts',)
        row = db.execute('SELECT skill_id,files_json,created_at,updated_at FROM skill_drafts').fetchone()
        assert row[0] == 's' and row[2:] == (11, 12)
        assert json.loads(row[1]) == {'SKILL.md': {'encoding': 'utf-8', 'content': body}}
        assert db.execute('SELECT revision FROM skills').fetchone() == (8,)
        assert db.execute('SELECT profile_id FROM runtime_profiles').fetchone() == ('p',)
        assert db.execute('SELECT bundle_ref FROM skill_versions').fetchone() == ('old-ref',)
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        assert not validate_required_schema(db)
    migrate('sqlite+aiosqlite:///' + str(path))

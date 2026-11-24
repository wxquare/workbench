import sqlite3

from workbench_agent.cli.db import migrate
from workbench_agent.persistence.sqlite_diagnostics import validate_required_schema


def test_review_indexes_upgrade_preserves_data(config_data):
    from pathlib import Path

    path = Path(config_data['storage']['persistent_root']) / 'db.sqlite3'
    migrate(config_data['storage']['database_url'])
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO teams(team_id,name,status) VALUES ('t','team','active')")
        db.execute("INSERT INTO agents(team_id,agent_id,name) VALUES ('t','a','Agent')")
    migrate(config_data['storage']['database_url'])
    with sqlite3.connect(path) as db:
        indexes = {
            row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        assert {
            'ix_review_agents_team_status',
            'ix_review_runs_team_agent_status_waiting',
            'ix_review_interrupts_team_status_actor',
            'ix_review_actions_team_state_created',
            'ix_review_approvals_team_subject_created',
        } <= indexes
        assert db.execute('SELECT version_num FROM alembic_version').fetchone() == (
            '0007_review_indexes',
        )
        assert db.execute('SELECT agent_id FROM agents').fetchone() == ('a',)
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        assert not validate_required_schema(db)

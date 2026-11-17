"""Historical action policy upgrade uses historical rows, never Agent runtime."""
import sqlite3

from test_storage_boundary_migration import seed_historical_run, upgrade_historical


def test_upgrade_0012_keeps_action_and_leaves_policy_uninitialized(tmp_path):
    path = tmp_path / 'legacy.db'
    upgrade_historical(path, '0012_schedule_manual_fires')
    seed_historical_run(path)
    with sqlite3.connect(path) as db:
        db.execute("""INSERT INTO actions(team_id,action_id,run_id,action_key,tool_version,payload_hash,
            payload_ref,scope,state,revision,created_at,updated_at)
            VALUES ('team','action','run','legacy','legacy-v1',?,'{}','old','unknown',0,10,10)""", ('a'*64,))
        before = db.execute('SELECT * FROM actions').fetchall()
    upgrade_historical(path)
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT * FROM actions').fetchall() == before
        assert db.execute('SELECT count(*) FROM action_policy_state').fetchone() == (0,)
        assert db.execute('SELECT version_num FROM alembic_version').fetchone() == ('0021_synthetic_batch_policy',)
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []

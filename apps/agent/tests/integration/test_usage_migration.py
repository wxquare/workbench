"""Usage upgrades preserve raw historical rows and refuse ambiguous history."""
import sqlite3

import pytest

from workbench_agent.persistence.schema_v4 import metadata as old
from test_storage_boundary_migration import seed_historical_run, snapshot, upgrade_historical


def legacy(path, status=None):
    upgrade_historical(path, '0005_resume_protocol')
    seed_historical_run(path)
    with sqlite3.connect(path) as db:
        db.execute("""INSERT INTO graph_checkpoints(team_id,run_id,namespace_hash,namespace,checkpoint_id,
            payload_type,payload,metadata_type,metadata,codec_version,run_epoch,created_at)
            VALUES ('team','run',?,'','old-checkpoint','bytes',?,'bytes',?,'1',0,1234)""",
                   ('0'*64, b'old typed checkpoint', b'old metadata'))
        if status == 'record':
            db.execute("""INSERT INTO usage_records(team_id,record_id,run_id,invocation,pricing_version,status)
                VALUES ('team','record','run','legacy','unknown','pending')""")
        elif status is not None:
            db.execute("""INSERT INTO usage_reservations(team_id,reservation_id,run_id,invocation,tokens,pricing_version,status)
                VALUES ('team','reservation','run','legacy',0,'unknown',?)""", (status,))


def test_usage_upgrade_without_usage_preserves_all_legacy_rows(tmp_path):
    path = tmp_path / 'legacy.db'
    legacy(path)
    with sqlite3.connect(path) as db:
        before = {name: db.execute('SELECT * FROM "'+name+'"').fetchall() for name in old.tables}
    upgrade_historical(path)
    with sqlite3.connect(path) as db:
        for name, rows in before.items():
            columns = ','.join('"'+column.name+'"' for column in old.tables[name].columns)
            assert db.execute('SELECT '+columns+' FROM "'+name+'"').fetchall() == rows
        assert db.execute('SELECT version_num FROM alembic_version').fetchone() == ('0021_synthetic_batch_policy',)
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
    assert 'model_attempts' not in old.tables['usage_reservations'].c


@pytest.mark.parametrize('status', ['reserved', 'cancelled', 'record'])
def test_ambiguous_legacy_usage_fails_before_any_ddl(tmp_path, status):
    path = tmp_path / 'legacy.db'
    legacy(path, status)
    before = snapshot(path)
    with pytest.raises(RuntimeError, match='ambiguous legacy usage.*offline reconciliation'):
        upgrade_historical(path)
    assert snapshot(path) == before


def test_upgraded_run_does_not_invent_a_budget_policy(tmp_path):
    path = tmp_path / 'legacy.db'
    legacy(path)
    upgrade_historical(path)
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT run_id FROM runs').fetchall() == [('run',)]
        assert db.execute('SELECT count(*) FROM usage_budget_policies').fetchone() == (0,)
        assert db.execute('SELECT count(*) FROM usage_reservations').fetchone() == (0,)


def test_tool_draft_upgrade_preserves_0006_data(tmp_path):
    path = tmp_path / 'legacy.db'
    upgrade_historical(path, '0006_usage_ledger')
    seed_historical_run(path)
    with sqlite3.connect(path) as db:
        before = db.execute('SELECT * FROM teams').fetchall()
    upgrade_historical(path)
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT * FROM teams').fetchall() == before
        assert db.execute('SELECT version_num FROM alembic_version').fetchone() == ('0021_synthetic_batch_policy',)
        assert db.execute('SELECT COUNT(*) FROM tool_binding_drafts').fetchone() == (0,)

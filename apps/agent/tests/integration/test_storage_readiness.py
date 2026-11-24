import json
import sqlite3
import subprocess
import sys
import pytest
from workbench_agent.settings import Settings
from workbench_agent.cli.doctor import check_sqlite_runtime


def prepare(config_data, schema='dod-storage-agent-v1', codec='dod-checkpoint-v1', wal=True):
    from pathlib import Path

    path = Path(config_data['storage']['persistent_root']) / 'db.sqlite3'
    with sqlite3.connect(path) as db:
        if wal:
            db.execute('PRAGMA journal_mode=WAL')
        db.execute(
            'CREATE TABLE dod_storage_metadata (schema_version TEXT NOT NULL, checkpoint_codec_version TEXT NOT NULL)'
        )
        db.execute('INSERT INTO dod_storage_metadata VALUES (?,?)', (schema, codec))
    return path


def test_missing_database_is_unready_without_creating_it(config_data):
    from pathlib import Path

    path = Path(config_data['storage']['persistent_root']) / 'db.sqlite3'
    result = check_sqlite_runtime(Settings.model_validate(config_data))
    assert not result.ready
    assert not path.exists()


@pytest.mark.parametrize(
    'schema,codec,wal,reason',
    [
        ('old', 'dod-checkpoint-v1', True, 'schema'),
        ('dod-storage-agent-v1', 'old', True, 'codec'),
        ('dod-storage-agent-v1', 'dod-checkpoint-v1', False, 'journal_mode'),
    ],
)
def test_incompatible_storage_rejected(config_data, schema, codec, wal, reason):
    prepare(config_data, schema, codec, wal)
    result = check_sqlite_runtime(Settings.model_validate(config_data))
    assert not result.ready
    assert reason in ' '.join(result.errors)


def test_real_runtime_reports_versions_pragmas_and_redacted_path(config_data):
    from workbench_agent.cli.db import migrate
    from pathlib import Path

    migrate(config_data['storage']['database_url'])
    path = Path(config_data['storage']['persistent_root']) / 'db.sqlite3'
    result = check_sqlite_runtime(Settings.model_validate(config_data))
    assert result.ready, result.errors
    assert result.sqlite_version == sqlite3.sqlite_version
    assert result.pragmas == {
        'journal_mode': 'wal',
        'synchronous': 2,
        'foreign_keys': 1,
        'busy_timeout': 5000,
        'wal_autocheckpoint': 1000,
    }
    assert result.schema_version == 'dod-storage-agent-v1'
    assert result.checkpoint_codec_version == 'dod-checkpoint-v1'
    assert str(path) not in result.model_dump_json()


def test_old_linked_sqlite_is_unready(config_data, monkeypatch):
    prepare(config_data)
    monkeypatch.setattr(sqlite3, 'sqlite_version_info', (3, 51, 2))
    result = check_sqlite_runtime(Settings.model_validate(config_data))
    assert not result.ready
    assert '3.51.3' in ' '.join(result.errors)


def test_path_outside_declared_persistent_volume_rejected(config_data, tmp_path):
    prepare(config_data)
    config_data['storage']['persistent_root'] = str(tmp_path / 'different')
    result = check_sqlite_runtime(Settings.model_validate(config_data))
    assert not result.ready
    assert 'persistent' in ' '.join(result.errors)


def test_unmigrated_database_not_modified(config_data):
    from pathlib import Path

    path = Path(config_data['storage']['persistent_root']) / 'db.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('PRAGMA journal_mode=WAL')
    result = check_sqlite_runtime(Settings.model_validate(config_data))
    assert not result.ready
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []


def test_doctor_cli_unready_exit_and_no_credentials(config_data, tmp_path):
    import yaml

    path = tmp_path / 'local.yaml'
    path.write_text(yaml.safe_dump(config_data))
    process = subprocess.run(
        [sys.executable, '-m', 'workbench_agent.cli.main', 'doctor', '--config', str(path)],
        capture_output=True,
        text=True,
    )
    assert process.returncode == 1
    assert json.loads(process.stdout)['ready'] is False
    assert config_data['storage']['database_url'] not in process.stdout + process.stderr


@pytest.mark.parametrize(
    'pragma,value',
    [('foreign_keys', 0), ('synchronous', 1), ('busy_timeout', 1), ('wal_autocheckpoint', 1)],
)
def test_connection_pragma_drift_is_unready(config_data, monkeypatch, pragma, value):
    import workbench_agent.cli.doctor as doctor

    prepare(config_data)
    original = doctor.open_readonly_connection

    def drifted_connection(path):
        connection = original(path)
        connection.execute(f'PRAGMA {pragma}={value}')
        return connection

    monkeypatch.setattr(doctor, 'open_readonly_connection', drifted_connection)
    result = doctor.check_sqlite_runtime(Settings.model_validate(config_data))
    assert not result.ready
    assert pragma in ' '.join(result.errors)


@pytest.mark.parametrize('rows', [0, 2])
def test_metadata_requires_exactly_one_row(config_data, rows):
    path = prepare(config_data)
    with sqlite3.connect(path) as db:
        if rows == 0:
            db.execute('DELETE FROM dod_storage_metadata')
        else:
            db.execute(
                "INSERT INTO dod_storage_metadata VALUES ('dod-storage-agent-v1', 'dod-checkpoint-v1')"
            )
    result = check_sqlite_runtime(Settings.model_validate(config_data))
    assert not result.ready
    assert 'exactly one' in ' '.join(result.errors)


def test_doctor_connection_forbids_business_writes(config_data):
    from workbench_agent.cli.doctor import open_readonly_connection

    path = prepare(config_data)
    connection = open_readonly_connection(path)
    try:
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            connection.execute('DELETE FROM dod_storage_metadata')
    finally:
        connection.close()


def test_doctor_malformed_yaml_redacts_input(tmp_path):
    path = tmp_path / 'bad.yaml'
    path.write_text('secret: [sensitive-credential\n')
    process = subprocess.run(
        [sys.executable, '-m', 'workbench_agent.cli.main', 'doctor', '--config', str(path)],
        capture_output=True,
        text=True,
    )
    assert process.returncode == 1
    assert 'sensitive-credential' not in process.stdout + process.stderr
    assert json.loads(process.stdout)['ready'] is False


def test_metadata_only_database_is_unready_without_ddl(config_data):
    path = prepare(config_data)
    result = check_sqlite_runtime(Settings.model_validate(config_data))
    assert not result.ready
    assert 'migration revision missing' in ' '.join(result.errors)
    with sqlite3.connect(path) as db:
        assert {
            row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        } == {'dod_storage_metadata'}


def test_migrated_schema_ready_and_missing_constraint_unready(config_data):
    from workbench_agent.cli.db import migrate

    migrate(config_data['storage']['database_url'])
    settings = Settings.model_validate(config_data)
    assert check_sqlite_runtime(settings).ready
    from pathlib import Path

    path = Path(config_data['storage']['persistent_root']) / 'db.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('DROP INDEX ix_claimable_jobs')
    assert not check_sqlite_runtime(settings).ready

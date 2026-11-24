import sqlite3
import pytest
from support.interrupts import InterruptPlatform


@pytest.mark.asyncio
async def test_online_wal_backup_and_quarantined_restore(platform, tmp_path):
    from workbench_agent.persistence.backends.sqlite_backup import SQLiteBackup, restore

    p = InterruptPlatform(platform)
    run, w = await p.waiting_run()
    source = platform.settings.storage.database_path
    copy = tmp_path / 'snapshot.sqlite3'
    target = tmp_path / 'restored.sqlite3'
    manifest = SQLiteBackup(source).create(copy)
    assert manifest.checkpoints > 0 and manifest.pending_writes > 0
    # Backup is usable while the live DB advances independently.
    await p.resume(run, w, key='after-backup')
    report = restore(copy, target)
    assert not report.external_writes_enabled and not report.dispatcher_enabled
    with sqlite3.connect(target) as db:
        assert db.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        assert db.execute('SELECT count(*) FROM resume_requests').fetchone() == (0,)
        assert db.execute('SELECT status FROM runs').fetchone() == ('waiting_input',)
    from workbench_agent.persistence.backends.sqlite import SQLiteBackend

    quarantined = SQLiteBackend('sqlite+aiosqlite:///' + str(target))
    assert not await quarantined.health()
    await quarantined.close()
    with pytest.raises(FileExistsError):
        restore(copy, target)
    with pytest.raises(FileExistsError):
        SQLiteBackup(source).create(copy)


@pytest.mark.asyncio
@pytest.mark.parametrize('corruption', ['parent', 'pending', 'codec', 'namespace', 'artifact'])
async def test_restore_rejects_corrupt_graph_or_artifact_without_target(
    platform, tmp_path, corruption
):
    from workbench_agent.persistence.backends.sqlite_backup import SQLiteBackup, restore

    p = InterruptPlatform(platform)
    await p.waiting_run()
    copy = tmp_path / 'copy.sqlite3'
    target = tmp_path / 'target.sqlite3'
    SQLiteBackup(platform.settings.storage.database_path).create(copy)
    with sqlite3.connect(copy) as db:
        if corruption == 'parent':
            db.execute("UPDATE graph_checkpoints SET parent_id='missing'")
        elif corruption == 'pending':
            db.execute("UPDATE graph_pending_writes SET checkpoint_id='missing'")
        elif corruption == 'codec':
            db.execute("UPDATE graph_checkpoints SET codec_version='unknown'")
        elif corruption == 'namespace':
            db.execute("UPDATE graph_checkpoints SET namespace='wrong'")
        else:
            db.execute(
                "INSERT INTO artifacts(team_id,artifact_id,run_id,kind,object_key,sha256,size,status) SELECT team_id,'00000000-0000-0000-0000-000000000099',run_id,'evidence','','bad',1,'available' FROM runs"
            )
    with pytest.raises(ValueError):
        restore(copy, target)
    assert not target.exists()


@pytest.mark.asyncio
async def test_restore_blocks_writes_with_unknown_action_and_keeps_receipt(platform, tmp_path):
    from workbench_agent.persistence.backends.sqlite_backup import SQLiteBackup, restore
    from workbench_agent.persistence.backends.sqlite import SQLiteBackend
    from workbench_agent.persistence.errors import StorageError
    from sqlalchemy import insert
    from workbench_agent.persistence import schema as s
    from uuid import uuid4

    run = await platform.create_run()
    action = str(uuid4())
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(
            insert(s.actions).values(
                team_id=platform.auth.team_id,
                run_id=run.run_id,
                action_id=action,
                action_key='unknown-delivery',
                tool_version='future@1',
                payload_hash='a' * 64,
                payload_ref='{}',
                scope='{}',
                state='unknown',
                receipt='uncertain',
            )
        )
    copy = tmp_path / 'copy.sqlite3'
    target = tmp_path / 'target.sqlite3'
    SQLiteBackup(platform.settings.storage.database_path).create(copy)
    report = restore(copy, target)
    assert report.manifest.actions == 1 and not report.dispatcher_enabled
    backend = SQLiteBackend('sqlite+aiosqlite:///' + str(target))
    with pytest.raises(StorageError):
        async with backend.create_uow().begin_write():
            pass
    with sqlite3.connect(target) as db:
        assert db.execute('SELECT state,receipt FROM actions').fetchone() == (
            'unknown',
            'uncertain',
        )
    await backend.close()


@pytest.mark.asyncio
async def test_explicit_clean_restore_acknowledgment_reopens_real_worker_and_api(
    platform, tmp_path
):
    from workbench_agent.persistence.backends.sqlite_backup import (
        SQLiteBackup,
        restore,
        acknowledge_restore,
    )
    from workbench_agent.persistence.backends.sqlite import SQLiteBackend
    from workbench_agent.runtime.graphs.registry import foundation_worker_registry
    from workbench_agent.jobs.worker import Worker
    from workbench_agent.api.app import create_app
    from httpx import AsyncClient, ASGITransport

    run = await platform.create_run()
    copy = tmp_path / 'copy.sqlite3'
    target = tmp_path / 'restored.sqlite3'
    SQLiteBackup(platform.settings.storage.database_path).create(copy)
    restore(copy, target)
    with pytest.raises(ValueError):
        acknowledge_restore(target, acknowledge=False)
    report = acknowledge_restore(target, acknowledge=True)
    assert report['external_writes_enabled'] and not report['dispatcher_enabled']
    backend = SQLiteBackend('sqlite+aiosqlite:///' + str(target))
    assert await backend.health()
    from support.model import install_test_model

    install_test_model(backend)
    assert (
        await Worker(backend, 'restored-worker', foundation_worker_registry(backend)).run_once()
        == 'projected'
    )
    settings = platform.settings.model_copy(deep=True)
    from pydantic import SecretStr

    settings.storage.database_url = SecretStr('sqlite+aiosqlite:///' + str(target))
    app = create_app(settings, backend=backend)
    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url='http://restored',
            headers={
                'Authorization': f'Bearer development:{platform.auth.team_id}:{platform.auth.subject}'
            },
        ) as client:
            assert (await client.get('/health/ready')).status_code == 200
            response = await client.get('/v1/runs/' + run.run_id)
            assert response.status_code == 200 and response.json()['status'] == 'succeeded'
    await backend.close()


@pytest.mark.asyncio
async def test_action_copy_cannot_be_reopened_by_clean_m1_acknowledgment(platform, tmp_path):
    from workbench_agent.persistence.backends.sqlite_backup import (
        SQLiteBackup,
        restore,
        acknowledge_restore,
    )
    from sqlalchemy import insert
    from workbench_agent.persistence import schema as s
    from uuid import uuid4

    run = await platform.create_run()
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(
            insert(s.actions).values(
                team_id=platform.auth.team_id,
                run_id=run.run_id,
                action_id=str(uuid4()),
                action_key='unsafe',
                tool_version='later@1',
                payload_hash='a' * 64,
                payload_ref='{}',
                scope='{}',
                state='unknown',
            )
        )
    copy = tmp_path / 'copy.sqlite3'
    target = tmp_path / 'target.sqlite3'
    SQLiteBackup(platform.settings.storage.database_path).create(copy)
    restore(copy, target)
    with pytest.raises(ValueError, match='offline reconciliation'):
        acknowledge_restore(target, acknowledge=True)
    assert target.with_name(target.name + '.restore-quarantine.json').exists()


@pytest.mark.asyncio
async def test_restore_publication_fsync_failure_keeps_existing_target_quarantined(
    platform, tmp_path, monkeypatch
):
    from workbench_agent.persistence.backends import sqlite_backup as backup

    source = tmp_path / 'source.sqlite3'
    target = tmp_path / 'restored.sqlite3'
    backup.SQLiteBackup(platform.settings.storage.database_path).create(source)
    real_sync = backup._sync_directory

    def fail_after_publish(path):
        if target.exists():
            raise OSError('injected directory fsync failure after publish')
        return real_sync(path)

    monkeypatch.setattr(backup, '_sync_directory', fail_after_publish)
    with pytest.raises(OSError, match='fsync failure'):
        backup.restore(source, target)
    assert target.exists() and backup.quarantine_path(target).exists()
    from workbench_agent.persistence.backends.sqlite import SQLiteBackend

    backend = SQLiteBackend('sqlite+aiosqlite:///' + str(target))
    assert not await backend.health()
    await backend.close()


@pytest.mark.asyncio
async def test_restore_rejects_special_pending_channel_with_positive_index(platform, tmp_path):
    from workbench_agent.persistence.backends.sqlite_backup import SQLiteBackup, restore

    p = InterruptPlatform(platform)
    await p.waiting_run()
    source = tmp_path / 'source.sqlite3'
    target = tmp_path / 'restored.sqlite3'
    SQLiteBackup(platform.settings.storage.database_path).create(source)
    with sqlite3.connect(source) as db:
        db.execute("UPDATE graph_pending_writes SET write_index=99 WHERE channel='__interrupt__'")
    with pytest.raises(ValueError, match='pending write index'):
        restore(source, target)
    assert not target.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('failed_sync', [1, 2])
async def test_acknowledgment_directory_failure_keeps_quarantine_and_review(
    platform, tmp_path, monkeypatch, failed_sync
):
    from workbench_agent.persistence.backends import sqlite_backup as backup

    source = tmp_path / 'source.sqlite3'
    target = tmp_path / 'restored.sqlite3'
    backup.SQLiteBackup(platform.settings.storage.database_path).create(source)
    backup.restore(source, target)
    real_sync = backup._sync_directory
    calls = 0

    def fail_directory_sync(path):
        nonlocal calls
        calls += 1
        if calls == failed_sync:
            raise OSError('injected acknowledgment directory failure')
        return real_sync(path)

    monkeypatch.setattr(backup, '_sync_directory', fail_directory_sync)
    with pytest.raises(OSError, match='acknowledgment directory failure'):
        backup.acknowledge_restore(target, acknowledge=True)
    assert backup.quarantine_path(target).exists()
    assert target.with_name(target.name + '.restore-review.json').exists()


@pytest.mark.asyncio
async def test_writer_waiting_behind_failed_acknowledgment_rechecks_quarantine(
    platform, tmp_path, monkeypatch
):
    import asyncio
    import threading
    from sqlalchemy import event, update
    from workbench_agent.persistence import schema as s
    from workbench_agent.persistence.backends import sqlite_backup as backup
    from workbench_agent.persistence.backends.sqlite import SQLiteBackend
    from workbench_agent.persistence.errors import StorageError

    source = tmp_path / 'source.sqlite3'
    target = tmp_path / 'restored.sqlite3'
    backup.SQLiteBackup(platform.settings.storage.database_path).create(source)
    backup.restore(source, target)
    backend = SQLiteBackend('sqlite+aiosqlite:///' + str(target))
    removed = threading.Event()
    begin_entered = threading.Event()
    committed = threading.Event()
    uow_entered = threading.Event()
    lock_states = []
    from contextlib import contextmanager
    from pathlib import Path

    owning_connection = []
    connection = backup._connection

    @contextmanager
    def capture_maintenance_connection(path, deadline, *, readonly=False):
        with connection(path, deadline, readonly=readonly) as db:
            if Path(path) == target and not readonly:
                owning_connection.append(db)
            yield db

    monkeypatch.setattr(backup, '_connection', capture_maintenance_connection)
    reinstate = backup._reinstate_quarantine

    def gated_reinstate(marker, quarantine):
        # Force the exact old window, not merely the writer's BEGIN entry.
        # An independent real SQLite probe proves whether maintenance unlocked.
        # Inspect the actual owning connection as well: a competing runtime
        # writer could itself own the lock after maintenance has released it.
        try:
            held = owning_connection[-1].in_transaction
        except sqlite3.ProgrammingError:
            held = False  # old connection already closed
        if held:
            with sqlite3.connect(target, timeout=0) as probe:
                with pytest.raises(sqlite3.OperationalError, match='locked'):
                    probe.execute('BEGIN IMMEDIATE')
        lock_states.append('held' if held else 'released')
        if not held:
            assert committed.wait(3), 'old unlock window did not admit waiting writer'
        else:
            assert not uow_entered.is_set()
        reinstate(marker, quarantine)

    monkeypatch.setattr(backup, '_reinstate_quarantine', gated_reinstate)
    real_sync = backup._sync_directory

    @event.listens_for(backend.engine.sync_engine, 'connect')
    def watch_begin(connection, _):
        def trace(statement):
            if statement == 'BEGIN IMMEDIATE':
                begin_entered.set()

        connection.run_async(lambda driver: driver.set_trace_callback(trace))

    def fail_ack_after_writer_enters(path):
        real_sync(path)
        if not backup.quarantine_path(target).exists():
            removed.set()
            assert begin_entered.wait(3), 'writer never entered SQLite BEGIN'
            assert not committed.is_set()  # Ack still owns SQLite's write lock.
            raise OSError('injected acknowledgment sync failure with waiting writer')

    monkeypatch.setattr(backup, '_sync_directory', fail_ack_after_writer_enters)

    async def writer():
        try:
            async with backend.create_uow().begin_write() as uow:
                uow_entered.set()
                await uow.execute(update(s.teams).values(name='must-not-commit'))
            committed.set()
            return 'committed'
        except StorageError:
            return 'blocked'

    acknowledgment = asyncio.create_task(
        asyncio.to_thread(backup.acknowledge_restore, target, acknowledge=True)
    )
    try:
        assert await asyncio.to_thread(removed.wait, 3)
        writing = asyncio.create_task(writer())
        with pytest.raises(OSError, match='waiting writer'):
            await acknowledgment
        assert await writing == 'blocked'
        assert lock_states == ['held'] and not uow_entered.is_set()
        assert begin_entered.is_set() and backup.quarantine_path(target).exists()
        with sqlite3.connect(target) as db:
            assert db.execute(
                "SELECT count(*) FROM teams WHERE name='must-not-commit'"
            ).fetchone() == (0,)
    finally:
        await backend.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['after-yield', 'after-close'])
async def test_committed_ack_cleanup_failure_reports_open_gate_without_reinstatement(
    platform, tmp_path, monkeypatch, phase
):
    import json
    from contextlib import contextmanager
    from pathlib import Path
    from workbench_agent.persistence.backends import sqlite_backup as backup

    source = tmp_path / 'source.sqlite3'
    target = tmp_path / 'target.sqlite3'
    backup.SQLiteBackup(platform.settings.storage.database_path).create(source)
    backup.restore(source, target)
    connection = backup._connection

    @contextmanager
    def broken_cleanup(path, deadline, *, readonly=False):
        with connection(path, deadline, readonly=readonly) as db:
            yield db
            if Path(path) == target and not readonly and phase == 'after-yield':
                raise sqlite3.OperationalError('injected helper cleanup failure after yield')
        if Path(path) == target and not readonly and phase == 'after-close':
            raise sqlite3.OperationalError('injected connection cleanup failure after close')

    monkeypatch.setattr(backup, '_connection', broken_cleanup)
    try:
        backup.acknowledge_restore(target, acknowledge=True)
    except Exception as error:
        assert type(error).__name__ == 'AcknowledgmentCleanupError'
        assert error.report['acknowledgment_committed'] is True
        assert error.report['cleanup_completed'] is False
        assert error.report['external_writes_enabled'] is True
        assert error.report['dispatcher_enabled'] is False
    else:
        pytest.fail('cleanup failure must be explicit')
    assert not backup.quarantine_path(target).exists()
    audit = json.loads(target.with_name(target.name + '.restore-review.json').read_text())
    assert audit['external_writes_enabled'] and not audit['dispatcher_enabled']


@pytest.mark.asyncio
async def test_acknowledgment_cli_distinguishes_committed_cleanup_failure(
    platform, tmp_path, monkeypatch, capsys
):
    import json
    import sys
    import yaml
    from contextlib import contextmanager
    from pathlib import Path
    from workbench_agent.persistence.backends import sqlite_backup as backup
    from workbench_agent.cli.main import main

    source = tmp_path / 'source.sqlite3'
    target = tmp_path / 'target.sqlite3'
    backup.SQLiteBackup(platform.settings.storage.database_path).create(source)
    backup.restore(source, target)
    config = tmp_path / 'config.yaml'
    data = platform.settings.model_dump(mode='json')
    data['storage']['database_url'] = platform.settings.storage.database_url.get_secret_value()
    config.write_text(yaml.safe_dump(data))
    connection = backup._connection

    @contextmanager
    def close_failure(path, deadline, *, readonly=False):
        with connection(path, deadline, readonly=readonly) as db:
            yield db
        if Path(path) == target and not readonly:
            raise sqlite3.OperationalError('injected close error')

    monkeypatch.setattr(backup, '_connection', close_failure)
    monkeypatch.setattr(
        sys,
        'argv',
        [
            'workbench',
            'db',
            'acknowledge-restore',
            '--config',
            str(config),
            '--target',
            str(target),
            '--acknowledge-no-external-effects',
        ],
    )
    with pytest.raises(SystemExit) as stopped:
        main()
    assert stopped.value.code == 1
    report = json.loads(capsys.readouterr().out)
    assert report['acknowledgment_committed'] and report['external_writes_enabled']
    assert not report['cleanup_completed'] and not report['dispatcher_enabled']
    assert not backup.quarantine_path(target).exists()

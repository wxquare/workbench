"""Maintenance owns one total five-second budget, including non-SQL work."""

import sqlite3
import time
from types import SimpleNamespace
import pytest
from support.interrupts import InterruptPlatform


def budget_clock(monkeypatch, backup):
    offset = [0.0]
    monkeypatch.setattr(
        backup,
        'time',
        SimpleNamespace(monotonic=lambda: time.monotonic() + offset[0], time=time.time),
    )
    return offset


@pytest.mark.asyncio
@pytest.mark.parametrize('operation', ['create', 'restore', 'acknowledge'])
async def test_cumulative_validation_and_filesystem_cost_cannot_reset_budget(
    platform, tmp_path, monkeypatch, operation
):
    from workbench_agent.persistence.backends import sqlite_backup as backup

    source = tmp_path / 'source.sqlite3'
    target = tmp_path / 'target.sqlite3'
    backup.SQLiteBackup(platform.settings.storage.database_path).create(source)
    if operation == 'acknowledge':
        backup.restore(source, target)
    offset = budget_clock(monkeypatch, backup)
    validate = backup.validate
    sync = backup._sync_directory

    def costly_validate(*args, **kwargs):
        result = validate(*args, **kwargs)
        offset[0] += 3
        return result

    def costly_sync(path):
        sync(path)
        offset[0] += 2.1

    monkeypatch.setattr(backup, 'validate', costly_validate)
    monkeypatch.setattr(backup, '_sync_directory', costly_sync)
    with pytest.raises(TimeoutError, match='deadline'):
        if operation == 'create':
            backup.SQLiteBackup(source).create(target)
        elif operation == 'restore':
            backup.restore(source, target)
        else:
            backup.acknowledge_restore(target, acknowledge=True)
    if operation != 'create' and target.exists():
        assert backup.quarantine_path(target).exists()


@pytest.mark.asyncio
async def test_restore_prevalidation_and_copy_validation_share_deadline(
    platform, tmp_path, monkeypatch
):
    from workbench_agent.persistence.backends import sqlite_backup as backup

    source = tmp_path / 'source.sqlite3'
    target = tmp_path / 'target.sqlite3'
    backup.SQLiteBackup(platform.settings.storage.database_path).create(source)
    offset = budget_clock(monkeypatch, backup)
    validate = backup.validate

    def costly_validate(*args, **kwargs):
        result = validate(*args, **kwargs)
        offset[0] += 2.6
        return result

    monkeypatch.setattr(backup, 'validate', costly_validate)
    with pytest.raises(TimeoutError, match='deadline'):
        backup.restore(source, target)
    assert not target.exists()  # second validation exhausted the SAME budget before publication


@pytest.mark.asyncio
async def test_acknowledgment_lock_wait_uses_only_remaining_budget(platform, tmp_path, monkeypatch):
    from workbench_agent.persistence.backends import sqlite_backup as backup

    source = tmp_path / 'source.sqlite3'
    target = tmp_path / 'target.sqlite3'
    backup.SQLiteBackup(platform.settings.storage.database_path).create(source)
    backup.restore(source, target)
    offset = budget_clock(monkeypatch, backup)
    validate = backup.validate

    def costly_validate(*args, **kwargs):
        result = validate(*args, **kwargs)
        offset[0] += 4.8
        return result

    monkeypatch.setattr(backup, 'validate', costly_validate)
    with sqlite3.connect(target) as lock:
        lock.execute('BEGIN IMMEDIATE')
        started = time.monotonic()
        with pytest.raises((TimeoutError, sqlite3.OperationalError)):
            backup.acknowledge_restore(target, acknowledge=True)
        elapsed = time.monotonic() - started
        assert elapsed < 0.8, f'lock wait reset the total budget: {elapsed:.3f}s'
        lock.rollback()
    assert backup.quarantine_path(target).exists()


@pytest.mark.asyncio
async def test_python_checkpoint_decoding_cannot_publish_after_deadline(
    platform, tmp_path, monkeypatch
):
    from workbench_agent.persistence.backends import sqlite_backup as backup

    p = InterruptPlatform(platform)
    await p.waiting_run()
    offset = budget_clock(monkeypatch, backup)
    decode = backup.CheckpointCodec.loads_typed

    def costly_decode(self, value):
        result = decode(self, value)
        offset[0] += 1
        return result

    monkeypatch.setattr(backup.CheckpointCodec, 'loads_typed', costly_decode)
    target = tmp_path / 'target.sqlite3'
    with pytest.raises(TimeoutError, match='deadline'):
        backup.SQLiteBackup(platform.settings.storage.database_path).create(target)
    assert not target.exists()


@pytest.mark.asyncio
async def test_hash_processing_cannot_publish_after_deadline(platform, tmp_path, monkeypatch):
    from workbench_agent.persistence.backends import sqlite_backup as backup

    offset = budget_clock(monkeypatch, backup)
    sha256 = backup.hashlib.sha256

    class CostlyHash:
        def __init__(self, data=b''):
            self.inner = sha256(data)
            if data:
                offset[0] += 5.1

        def update(self, data):
            self.inner.update(data)
            offset[0] += 5.1

        def hexdigest(self):
            return self.inner.hexdigest()

    monkeypatch.setattr(backup, 'hashlib', SimpleNamespace(sha256=CostlyHash))
    target = tmp_path / 'target.sqlite3'
    with pytest.raises(TimeoutError, match='deadline'):
        backup.SQLiteBackup(platform.settings.storage.database_path).create(target)
    assert not target.exists()


@pytest.mark.asyncio
async def test_expiry_after_unquarantine_recreates_gate_instead_of_reporting_success(
    platform, tmp_path, monkeypatch
):
    from workbench_agent.persistence.backends import sqlite_backup as backup

    source = tmp_path / 'source.sqlite3'
    target = tmp_path / 'target.sqlite3'
    backup.SQLiteBackup(platform.settings.storage.database_path).create(source)
    backup.restore(source, target)
    offset = budget_clock(monkeypatch, backup)
    sync = backup._sync_directory

    def expire_after_removal(path):
        sync(path)
        if not backup.quarantine_path(target).exists():
            offset[0] += 5.1

    monkeypatch.setattr(backup, '_sync_directory', expire_after_removal)
    with pytest.raises(TimeoutError, match='deadline'):
        backup.acknowledge_restore(target, acknowledge=True)
    assert backup.quarantine_path(target).exists()


@pytest.mark.asyncio
async def test_python_parent_chain_walk_stops_at_shared_deadline(platform, tmp_path, monkeypatch):
    from workbench_agent.persistence.backends import sqlite_backup as backup

    p = InterruptPlatform(platform)
    await p.waiting_run()
    offset = budget_clock(monkeypatch, backup)
    visited = []

    class CostlyVisited(set):
        def add(self, value):
            super().add(value)
            visited.append(value)
            offset[0] += 3

    monkeypatch.setattr(backup, 'set', CostlyVisited, raising=False)
    target = tmp_path / 'target.sqlite3'
    with pytest.raises(TimeoutError, match='deadline'):
        backup.SQLiteBackup(platform.settings.storage.database_path).create(target)
    assert len(visited) <= 2 and not target.exists()


@pytest.mark.asyncio
async def test_restore_deadline_after_atomic_publication_preserves_quarantine(
    platform, tmp_path, monkeypatch
):
    from workbench_agent.persistence.backends import sqlite_backup as backup

    source = tmp_path / 'source.sqlite3'
    target = tmp_path / 'target.sqlite3'
    backup.SQLiteBackup(platform.settings.storage.database_path).create(source)
    offset = budget_clock(monkeypatch, backup)
    sync = backup._sync_directory

    def expire_published_target(path):
        sync(path)
        if target.exists():
            offset[0] += 5.1

    monkeypatch.setattr(backup, '_sync_directory', expire_published_target)
    with pytest.raises(TimeoutError, match='deadline'):
        backup.restore(source, target)
    assert target.exists() and backup.quarantine_path(target).exists()


@pytest.mark.asyncio
async def test_acknowledgment_logical_commit_precedes_connection_cleanup_budget(
    platform, tmp_path, monkeypatch
):
    from contextlib import contextmanager
    from pathlib import Path
    from workbench_agent.persistence.backends import sqlite_backup as backup

    source = tmp_path / 'source.sqlite3'
    target = tmp_path / 'target.sqlite3'
    backup.SQLiteBackup(platform.settings.storage.database_path).create(source)
    backup.restore(source, target)
    offset = budget_clock(monkeypatch, backup)
    connection = backup._connection
    cleanup = []

    @contextmanager
    def slow_connection_cleanup(path, deadline, *, readonly=False):
        with connection(path, deadline, readonly=readonly) as db:
            yield db
            if Path(path) == target and not readonly:
                assert not backup.quarantine_path(target).exists()
                cleanup.append('after-yield-before-close')
                offset[0] += 5.1
        if Path(path) == target and not readonly:
            cleanup.append('after-close')
            offset[0] += 5.1

    monkeypatch.setattr(backup, '_connection', slow_connection_cleanup)
    report = backup.acknowledge_restore(target, acknowledge=True)
    assert report['external_writes_enabled'] and not report['dispatcher_enabled']
    assert cleanup == ['after-yield-before-close', 'after-close']
    assert not backup.quarantine_path(target).exists()

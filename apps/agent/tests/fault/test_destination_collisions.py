"""Destination collisions must survive backup/restore byte-for-byte."""

from pathlib import Path
import shutil
import sqlite3
import pytest
from workbench_agent.persistence.backends.sqlite_backup import (
    SQLiteBackup,
    restore,
    quarantine_path,
)


@pytest.mark.parametrize('operation', ['create', 'restore'])
@pytest.mark.parametrize('suffix', ['', '-wal', '-shm', '-journal'])
@pytest.mark.parametrize('kind', ['file', 'symlink', 'dangling'])
def test_sqlite_destination_collision_is_preserved(platform, tmp_path, operation, suffix, kind):
    target = tmp_path / 'collision.sqlite3'
    companion = Path(str(target) + suffix)
    backing = tmp_path / 'backing'
    backing.write_bytes(b'preserve collision')
    if kind == 'file':
        companion.write_bytes(b'preserve collision')
    else:
        companion.symlink_to(backing if kind == 'symlink' else tmp_path / 'absent')
    source = platform.settings.storage.database_path
    with pytest.raises(FileExistsError):
        SQLiteBackup(source).create(target) if operation == 'create' else restore(source, target)
    assert (
        companion.is_symlink()
        if kind != 'file'
        else companion.read_bytes() == b'preserve collision'
    )
    assert backing.read_bytes() == b'preserve collision'
    assert not quarantine_path(target).exists()
    if suffix:
        assert not target.exists()


@pytest.mark.parametrize('operation', ['create', 'restore'])
def test_real_leftover_wal_cannot_replace_published_snapshot(platform, tmp_path, operation):
    source = platform.settings.storage.database_path
    stale = tmp_path / 'stale.sqlite3'
    SQLiteBackup(source).create(stale)
    db = sqlite3.connect(stale)
    try:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA wal_autocheckpoint=0')
        db.execute("UPDATE teams SET name='STALE_TARGET_WAL'")
        db.commit()
        target = tmp_path / 'target.sqlite3'
        # Copy a real committed, uncheckpointed WAL while its owner is open.
        wal = Path(str(target) + '-wal')
        shutil.copyfile(Path(str(stale) + '-wal'), wal)
        before = wal.read_bytes()
        assert len(before) > 32
        witness = tmp_path / 'replay-witness.sqlite3'
        SQLiteBackup(source).create(witness)
        Path(str(witness) + '-wal').write_bytes(before)
        with sqlite3.connect(witness) as replay:
            assert replay.execute('SELECT DISTINCT name FROM teams').fetchall() == [
                ('STALE_TARGET_WAL',)
            ]
        with pytest.raises(FileExistsError):
            SQLiteBackup(source).create(target) if operation == 'create' else restore(
                source, target
            )
        assert not target.exists() and wal.read_bytes() == before
        assert not quarantine_path(target).exists()
    finally:
        db.close()

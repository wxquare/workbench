"""Consistent snapshots and fail-closed restore within one maintenance deadline."""

from dataclasses import dataclass
from pathlib import Path
from contextlib import contextmanager
import hashlib
import json
import os
import sqlite3
import tempfile
import time
from workbench_agent.persistence.checkpoints.codec import CheckpointCodec
from workbench_agent.persistence.checkpoints.store import checkpoint_namespace_key
from workbench_agent.persistence.sqlite_diagnostics import validate_required_schema


def quarantine_path(path):
    return Path(str(path) + '.restore-quarantine.json')


@dataclass(frozen=True)
class _Deadline:
    expires_at: float

    @classmethod
    def start(cls):
        return cls(time.monotonic() + 5)

    def remaining(self):
        remaining = self.expires_at - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('maintenance storage deadline exceeded')
        return remaining

    def check(self):
        self.remaining()

    def constrain(self, db):
        # Recompute before every statement; a previous phase never grants a new
        # busy_timeout. Progress handles SQL work between these statement checks.
        db.execute(f'PRAGMA busy_timeout={max(0, int(self.remaining() * 1000))}')
        self.check()


@contextmanager
def _connection(path, deadline, *, readonly=False):
    db = None
    try:
        db = sqlite3.connect(
            Path(path).absolute().as_uri() + ('?mode=ro' if readonly else '?mode=rw'),
            uri=True,
            timeout=deadline.remaining(),
        )
        deadline.check()
        db.set_progress_handler(lambda: int(time.monotonic() >= deadline.expires_at), 100)
        for sql in (
            'PRAGMA foreign_keys=ON',
            'PRAGMA synchronous=FULL',
            'PRAGMA wal_autocheckpoint=1000',
        ):
            _query(db, deadline, sql)
        yield db
    except sqlite3.Error:
        deadline.check()  # Normalize an expired SQLite progress callback to timeout.
        raise
    finally:
        if db is not None:
            db.close()


def _query(db, deadline, sql):
    deadline.constrain(db)
    rows = db.execute(sql).fetchall()
    deadline.check()
    return rows


def _hash_file(path, deadline):
    digest = hashlib.sha256()
    deadline.check()
    with Path(path).open('rb') as file:
        while True:
            deadline.check()
            chunk = file.read(1024 * 1024)
            deadline.check()
            if not chunk:
                break
            digest.update(chunk)
            deadline.check()
    result = digest.hexdigest()
    deadline.check()
    return result


@dataclass(frozen=True)
class BackupManifest:
    path: str
    sha256: str
    checkpoints: int
    pending_writes: int
    artifacts: int
    actions: int


@dataclass(frozen=True)
class RestoreReport:
    path: str
    manifest: BackupManifest
    external_writes_enabled: bool = False
    dispatcher_enabled: bool = False
    restriction: str = (
        'offline checkpoint/artifact/action reconciliation required before opening writes'
    )


def validate(path, *, deadline=None):
    deadline = deadline if deadline is not None else _Deadline.start()
    with _connection(path, deadline, readonly=True) as db:
        query = lambda sql: _query(db, deadline, sql)
        if query('PRAGMA integrity_check') != [('ok',)] or query('PRAGMA foreign_key_check'):
            raise ValueError('restore integrity/foreign key validation failed')
        if validate_required_schema(db, lambda: deadline.constrain(db)):
            raise ValueError('restore schema incompatible')
        deadline.check()
        db.row_factory = sqlite3.Row
        codec = CheckpointCodec()
        rows = query('SELECT * FROM graph_checkpoints')
        parents = {}
        for row in rows:
            deadline.check()
            key = (row['team_id'], row['run_id'], row['namespace_hash'], row['checkpoint_id'])
            if (
                row['codec_version'] != codec.version
                or checkpoint_namespace_key(row['namespace']) != row['namespace_hash']
            ):
                raise ValueError('restore checkpoint codec/namespace incompatible')
            value = codec.loads_typed((row['payload_type'], row['payload']))
            deadline.check()
            codec.loads_typed((row['metadata_type'], row['metadata']))
            deadline.check()
            if value.get('id') != row['checkpoint_id'] or not isinstance(
                value.get('channel_values'), dict
            ):
                raise ValueError('restore checkpoint identity invalid')
            parents[key] = (*key[:3], row['parent_id']) if row['parent_id'] else None
        for key in parents:
            deadline.check()
            seen = set()
            cursor = key
            while cursor is not None:
                deadline.check()
                if cursor in seen or cursor not in parents:
                    raise ValueError('restore checkpoint parent chain invalid')
                seen.add(cursor)
                cursor = parents[cursor]
                deadline.check()
        pending = query('SELECT * FROM graph_pending_writes')
        from langgraph.checkpoint.base import WRITES_IDX_MAP

        for row in pending:
            deadline.check()
            key = (row['team_id'], row['run_id'], row['namespace_hash'], row['checkpoint_id'])
            if key not in parents:
                raise ValueError('restore orphan pending write')
            if (row['write_index'] < 0 or row['channel'] in WRITES_IDX_MAP) and WRITES_IDX_MAP.get(
                row['channel']
            ) != row['write_index']:
                raise ValueError('restore pending write index incompatible')
            codec.loads_typed((row['payload_type'], row['payload']))
            deadline.check()
        artifacts = query('SELECT * FROM artifacts')
        for row in artifacts:
            deadline.check()
            if row['status'] == 'available' and (
                not row['object_key']
                or len(row['sha256']) != 64
                or any(c not in '0123456789abcdef' for c in row['sha256'])
                or row['size'] < 0
            ):
                raise ValueError('restore artifact metadata invalid')
        if query("""SELECT 1 FROM evidence e JOIN artifacts a ON a.artifact_id=e.artifact_ref
            WHERE a.team_id!=e.team_id OR (a.run_id IS NOT NULL AND a.run_id!=e.run_id) LIMIT 1"""):
            raise ValueError('restore artifact reference scope invalid')
        actions = query('SELECT count(*) FROM actions')[0][0]
        counts = len(rows), len(pending), len(artifacts), actions
    deadline.check()
    return counts


def _require_empty_destination(destination):
    """Never publish a SQLite main file beside another database's recovery files.

    lexists semantics preserve dangling symlinks as well as ordinary collisions.
    Maintenance requires an offline destination; repeat immediately before link.
    """
    for suffix in ('', '-wal', '-shm', '-journal'):
        entry = Path(str(destination) + suffix)
        if entry.exists() or entry.is_symlink():
            raise FileExistsError(entry)


class SQLiteBackup:
    def __init__(self, source_path):
        self.source_path = Path(source_path).absolute()

    def create(self, destination: Path) -> BackupManifest:
        deadline = _Deadline.start()
        manifest = self._create(destination, deadline)
        deadline.check()
        return manifest

    def _create(self, destination, deadline):
        destination = Path(destination).absolute()
        _require_empty_destination(destination)
        deadline.check()
        fd, name = tempfile.mkstemp(
            prefix='.dod-backup-', suffix='.sqlite3', dir=destination.parent
        )
        os.close(fd)
        temporary = Path(name)
        try:
            deadline.check()
            with (
                _connection(self.source_path, deadline, readonly=True) as source,
                _connection(temporary, deadline) as target,
            ):
                source.backup(
                    target,
                    pages=256,
                    progress=lambda *_: deadline.check(),
                    sleep=min(0.01, deadline.remaining()),
                )
                deadline.check()
            deadline.check()
            counts = validate(temporary, deadline=deadline)
            deadline.check()
            digest = _hash_file(temporary, deadline)
            with temporary.open('rb') as file:
                os.fsync(file.fileno())
            deadline.check()  # Blocking FS work is not preemptible; never publish after it expires.
            _require_empty_destination(destination)
            deadline.check()
            os.link(temporary, destination)  # atomic publish; never replace another destination
            deadline.check()
            _sync_directory(destination.parent)
            deadline.check()
            return BackupManifest(str(destination), digest, *counts)
        finally:
            # Rollback/cleanup may finish after expiry, but cannot turn it into success.
            temporary.unlink(missing_ok=True)
            for suffix in ('-wal', '-shm'):
                Path(str(temporary) + suffix).unlink(missing_ok=True)


def restore(source: Path, target: Path) -> RestoreReport:
    deadline = _Deadline.start()
    target = Path(target).absolute()
    _require_empty_destination(target)
    deadline.check()
    validate(Path(source), deadline=deadline)
    deadline.check()
    marker = quarantine_path(target)
    with marker.open('x') as file:
        json.dump(
            {
                'external_writes_enabled': False,
                'dispatcher_enabled': False,
                'action_reconciliation': 'required',
                'artifact_objects': 'not_verified',
            },
            file,
        )
        file.flush()
        os.fsync(file.fileno())
    deadline.check()
    _sync_directory(target.parent)
    deadline.check()
    try:
        manifest = SQLiteBackup(source)._create(target, deadline)
        deadline.check()
    except BaseException:
        # Publication can precede fsync failure/expiry. Never expose an uncertain
        # target by removing its gate. This operation exclusively created marker.
        if not target.exists() and not target.is_symlink():
            marker.unlink(missing_ok=True)
        raise
    return RestoreReport(str(target), manifest)


def _sync_directory(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _reinstate_quarantine(marker, quarantine):
    # Safety cleanup is required even after the work budget expired. Do not
    # overwrite/remove any marker another operation created in the meantime.
    try:
        with marker.open('xb') as file:
            file.write(quarantine)
            file.flush()
            os.fsync(file.fileno())
    except FileExistsError:
        pass
    _sync_directory(marker.parent)


class AcknowledgmentCleanupError(RuntimeError):
    """Logical acknowledgment committed; connection cleanup outcome is uncertain."""

    def __init__(self, report):
        super().__init__(
            'restore acknowledgment committed; connection cleanup failed; inspect durable review and open write gate'
        )
        self.report = dict(report, acknowledgment_committed=True, cleanup_completed=False)


def acknowledge_restore(target: Path, *, acknowledge=False):
    """Complete all fallible gate work under the maintenance write lock.

    The final checked point inside that lock commits the acknowledgment. Driver
    release afterward is cleanup only and cannot retroactively restore the gate.
    """
    deadline = _Deadline.start()
    target = Path(target).resolve()
    marker = quarantine_path(target)
    if not acknowledge or not marker.is_file():
        raise ValueError('explicit restore acknowledgment and quarantine required')
    deadline.check()
    with marker.open('rb') as file:
        quarantine = file.read(65537)
    deadline.check()
    if len(quarantine) > 65536:
        raise ValueError('restore quarantine record is oversized')
    counts = validate(target, deadline=deadline)
    deadline.check()
    committed = False
    try:
        with _connection(target, deadline) as db:
            _query(db, deadline, 'BEGIN IMMEDIATE')
            removed = False
            try:
                if (
                    counts[2]
                    or counts[3]
                    or _query(db, deadline, 'SELECT count(*) FROM outbox')[0][0]
                ):
                    raise ValueError(
                        'external artifacts/actions require offline reconciliation; M1 cannot release quarantine'
                    )
                report = {
                    'external_writes_enabled': True,
                    'dispatcher_enabled': False,
                    'review': 'explicit clean M1 acknowledgment',
                    'acknowledged_at_ms': int(time.time() * 1000),
                    'database_sha256_at_review': _hash_file(target, deadline),
                }
                audit = Path(str(target) + '.restore-review.json')
                deadline.check()
                with audit.open('x') as file:
                    json.dump(report, file, sort_keys=True)
                    file.flush()
                    os.fsync(file.fileno())
                deadline.check()
                _sync_directory(target.parent)
                deadline.check()
                marker.unlink()
                removed = True
                deadline.check()
                _sync_directory(target.parent)
                deadline.check()
            except BaseException:
                # Crucially, _connection has NOT exited or closed the owning
                # connection yet. Waiting runtime writers cannot pass their
                # post-BEGIN marker check until reinstatement is complete.
                if removed:
                    _reinstate_quarantine(marker, quarantine)
                raise
            committed = True  # Logical commit: final deadline/fsync checks passed while locked.
            # No rollback, SQL, deadline or filesystem work after this point.
            # _connection's close releases the lock as cleanup, with no new gate decision.
    except Exception as error:
        if committed:
            raise AcknowledgmentCleanupError(report) from error
        raise
    return report

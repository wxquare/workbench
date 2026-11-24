"""Read-only storage readiness. Does not migrate or probe paid/external tools."""

from __future__ import annotations

import sqlite3
import time

from pydantic import BaseModel, Field
from workbench_agent.settings import MIN_SQLITE_VERSION, Settings

from workbench_agent.persistence.sqlite_diagnostics import (
    METADATA_TABLE,
    EXPECTED_PRAGMAS,
    open_readonly_connection,
    validate_required_schema,
)


class StorageDiagnostics(BaseModel):
    ready: bool = False
    backend: str = 'sqlite'
    database_path: str = '<persistent-root>/<database>'
    sqlite_version: str = ''
    pragmas: dict[str, str | int] = Field(default_factory=dict)
    schema_version: str | None = None
    checkpoint_codec_version: str | None = None
    errors: list[str] = Field(default_factory=list)


from workbench_agent.persistence.readiness import persistent_path_errors as _persistent_path_errors


def check_sqlite_runtime(settings: Settings) -> StorageDiagnostics:
    result = StorageDiagnostics(sqlite_version=sqlite3.sqlite_version)
    if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:
        result.errors.append('linked SQLite runtime must be >=3.51.3')
    try:
        result.errors.extend(_persistent_path_errors(settings))
    except OSError:
        result.errors.append('persistent filesystem cannot be inspected')
    if result.errors:
        return result
    started = time.monotonic()
    connection = None
    try:
        connection = open_readonly_connection(settings.storage.database_path)
        # A single monotonic deadline includes lock waits and all SQL checks.
        connection.set_progress_handler(lambda: int(time.monotonic() - started >= 5), 100)
        # Read connection-local values before shortening the lock-wait timeout;
        # this detects drift rather than overwriting it during diagnostics.
        for name in ('synchronous', 'foreign_keys', 'busy_timeout', 'wal_autocheckpoint'):
            actual = connection.execute(f'PRAGMA {name}').fetchone()[0]
            result.pragmas[name] = actual
            if actual != EXPECTED_PRAGMAS[name]:
                result.errors.append(f'PRAGMA {name} is incompatible')

        def constrain_lock_wait() -> None:
            remaining = 5 - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError
            connection.execute(
                f'PRAGMA busy_timeout={max(0, min(100, int(remaining * 1000) - 150))}'
            )

        constrain_lock_wait()
        result.pragmas['journal_mode'] = connection.execute('PRAGMA journal_mode').fetchone()[0]
        if result.pragmas['journal_mode'] != 'wal':
            result.errors.append('PRAGMA journal_mode is incompatible')
        constrain_lock_wait()
        rows = connection.execute(
            'SELECT schema_version, checkpoint_codec_version FROM dod_storage_metadata LIMIT 2'
        ).fetchall()
        if len(rows) != 1:
            result.errors.append('schema metadata must contain exactly one canonical row')
        else:
            result.schema_version, result.checkpoint_codec_version = rows[0]
            if result.schema_version != settings.storage.schema_version:
                result.errors.append('schema version mismatch')
            if result.checkpoint_codec_version != settings.storage.checkpoint_codec_version:
                result.errors.append('checkpoint codec version mismatch')
        result.errors.extend(validate_required_schema(connection, constrain_lock_wait))
    except sqlite3.Error:
        result.errors.append('storage unavailable or schema metadata missing/incompatible')
    except TimeoutError:
        result.errors.append('storage diagnostic exceeded 5-second deadline')
    finally:
        if connection is not None:
            connection.close()
    if time.monotonic() - started > 5:
        result.errors.append('storage diagnostic exceeded 5-second deadline')
    result.ready = not result.errors
    return result

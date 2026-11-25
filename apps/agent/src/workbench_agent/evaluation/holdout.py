"""Explicit release gate for holdout data."""

from __future__ import annotations

import json
import fcntl
import os
from pathlib import Path

from workbench_agent.evaluation.journal import fsync_directory


def authorize_split(
    split: str, *, purpose: str | None, permit: dict | None, release_id: str | None
) -> None:
    if split != 'holdout':
        return
    if (
        purpose != 'release'
        or not isinstance(release_id, str)
        or not release_id.strip()
        or release_id != release_id.strip()
        or not isinstance(permit, dict)
    ):
        raise PermissionError('holdout requires release purpose and permit')
    if permit.get('release_id') != release_id or permit.get('active') is not True:
        raise PermissionError('holdout permit is invalid')


def append_holdout_access(ledger: str | Path, event: dict) -> None:
    path = Path(ledger)
    key = (event.get('release_id'), event.get('dataset_hash'))
    if not all(key):
        raise ValueError('holdout event requires release and dataset')
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, 'a+', encoding='utf-8') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.seek(0)
        contents = handle.read()
        if contents and not contents.endswith('\n'):
            raise ValueError('incomplete holdout access ledger')
        existing = [json.loads(line) for line in contents.splitlines() if line.strip()]
        if not all(
            isinstance(item, dict)
            and all(
                isinstance(item.get(field), str) and item[field]
                for field in ('release_id', 'dataset_hash')
            )
            for item in existing
        ):
            raise ValueError('invalid holdout access ledger')
        if not any((item.get('release_id'), item.get('dataset_hash')) == key for item in existing):
            handle.write(json.dumps(event, sort_keys=True) + '\n')
            handle.flush()
            os.fsync(handle.fileno())
    fsync_directory(path.parent)

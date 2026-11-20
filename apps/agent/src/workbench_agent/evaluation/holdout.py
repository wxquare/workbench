"""Explicit release gate for holdout data."""

from __future__ import annotations

import json
from pathlib import Path


def authorize_split(
    split: str, *, purpose: str | None, permit: dict | None, release_id: str | None
) -> None:
    if split != 'holdout':
        return
    if purpose != 'release' or not release_id or not isinstance(permit, dict):
        raise PermissionError('holdout requires release purpose and permit')
    if permit.get('release_id') != release_id or permit.get('active') is not True:
        raise PermissionError('holdout permit is invalid')


def append_holdout_access(ledger: str | Path, event: dict) -> None:
    path = Path(ledger)
    key = (event.get('release_id'), event.get('dataset_hash'))
    if not all(key):
        raise ValueError('holdout event requires release and dataset')
    existing = []
    if path.exists():
        existing = [
            json.loads(line)
            for line in path.read_text().splitlines()
            if line
        ]
    if not any(
        (item.get('release_id'), item.get('dataset_hash')) == key
        for item in existing
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a') as handle:
            handle.write(json.dumps(event, sort_keys=True) + '\n')
            handle.flush()
            import os
            os.fsync(handle.fileno())

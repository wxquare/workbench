"""Durable local JSON and JSONL records for resumable evaluation runs."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from workbench_agent.evaluation.contracts import canonical


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.tmp')
    with temporary.open('w', encoding='utf-8') as handle:
        handle.write(canonical(value))
        handle.write('\n')
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    fsync_directory(path.parent)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding='utf-8'))


class Journal:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event: dict) -> None:
        if not isinstance(event, dict):
            raise ValueError('journal events must be objects')
        with self.path.open('a', encoding='utf-8') as handle:
            handle.write(canonical(event))
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())

    def events(self) -> list[dict]:
        if not self.path.exists():
            return []
        result = []
        for line in self.path.read_text(encoding='utf-8').splitlines():
            if line:
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError('journal contains a non-object event')
                result.append(value)
        return result

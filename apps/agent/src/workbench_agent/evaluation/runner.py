"""Serial, resumable evaluation experiment coordinator."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from workbench_agent.evaluation.collector import collect_case, read_rows, write_rows
from workbench_agent.evaluation.dataset import (
    dataset_fingerprint,
    select_cases,
    validate_manifest,
)
from workbench_agent.evaluation.journal import Journal, atomic_json, fsync_directory


class ExperimentLocked(RuntimeError):
    pass


class ExperimentDirectory:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.path / '.runner.lock'
        self._fd = None

    def __enter__(self):
        try:
            self._fd = os.open(
                self.lock_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError:
            raise ExperimentLocked('experiment directory is already being written') from None
        os.write(self._fd, f'{os.getpid()}\n'.encode())
        os.fsync(self._fd)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
            try:
                self.lock_path.unlink()
            except FileNotFoundError:
                pass
        return False


def _read_manifest(path: Path) -> dict:
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError('experiment manifest must be an object')
    return value


def _config_hash(config: dict) -> str:
    behavior = {
        key: value for key, value in config.items() if key not in {'token_env', 'output_root'}
    }
    return hashlib.sha256(
        json.dumps(
            behavior,
            sort_keys=True,
            separators=(',', ':'),
            ensure_ascii=False,
        ).encode()
    ).hexdigest()


def _seed_hash(config: dict) -> str:
    path = Path(config['seed_manifest_path'])
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest(
    dataset: dict,
    config: dict,
    *,
    experiment_id: str,
    split: str,
    repeats: int,
    git_commit: str | None,
    comparability: str,
) -> dict:
    return {
        'experiment_id': experiment_id,
        'dataset_hash': dataset_fingerprint(dataset),
        'config_hash': _config_hash(config),
        'seed_hash': _seed_hash(config),
        'git_commit': git_commit,
        'graph_id': config['expected_graph_id'],
        'profile_ref': config.get('expected_profile_ref'),
        'model_refs': config.get('expected_model_refs') or [],
        'kb_versions': config.get('expected_kb_versions') or [],
        'repeats': repeats,
        'split': split,
        'started_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'runner_version': 'knowledge-runner-v1',
        'scoring_version': 'knowledge-rules-v1',
        'limits': config['limits'],
        'comparability': comparability,
    }


async def run_experiment(
    dataset: dict,
    config: dict,
    *,
    manifest: dict,
    journal,
    api,
    split: str,
    repeats: int,
    deadline_seconds: float,
) -> dict:
    if split not in {'dev', 'holdout'}:
        raise ValueError('unsupported split')
    if type(repeats) is not int or not 1 <= repeats <= 5:
        raise ValueError('repeats must be between 1 and 5')
    if manifest['dataset_hash'] != dataset_fingerprint(dataset):
        raise ValueError('dataset does not match manifest')
    if manifest['config_hash'] != _config_hash(config):
        raise ValueError('config does not match manifest')
    cases = select_cases(dataset, split)
    output_path = Path(journal.path).with_name('results.jsonl')
    existing = {(row['case_id'], row['repeat']): row for row in read_rows(output_path)}
    for event in journal.events():
        request_key = event.get('request_key')
        if isinstance(event.get('row'), dict):
            row = event['row']
            case_id = event.get('case_id', row.get('case_id'))
            repeat = event.get('repeat', row.get('repeat'))
            if case_id is not None and repeat is not None:
                existing[(case_id, int(repeat))] = row
        elif event.get('event') == 'create_sent' and request_key:
            case_id = event.get('case_id')
            repeat = event.get('repeat')
            if case_id is not None and repeat is not None:
                existing[(case_id, int(repeat))] = {
                    'case_id': case_id,
                    'repeat': int(repeat),
                    'request_key': request_key,
                    'run_id': None,
                    'transport_status': 'needs_reconciliation',
                    'error_code': 'ACCEPTANCE_UNKNOWN',
                }
    for case in cases:
        for repeat in range(repeats):
            recovered = existing.get((case['id'], repeat))
            if (
                recovered
                and recovered.get('run_id')
                and recovered.get('run_status')
                in {
                    'succeeded',
                    'partial',
                    'failed',
                    'cancelled',
                    'timed_out',
                    'expired',
                }
            ):
                continue
            deadline = time.monotonic() + deadline_seconds
            row = await collect_case(
                api,
                case,
                agent_id=config['agent_id'],
                experiment={
                    **manifest,
                    'config': config,
                    'recovered_rows': existing,
                },
                repeat=repeat,
                journal=journal,
                deadline=deadline,
            )
            existing[(case['id'], repeat)] = row
            write_rows(output_path, list(existing.values()))
    rows = list(existing.values())
    return {
        'complete': all(row.get('transport_status') in {'completed', 'rejected'} for row in rows)
        and len(rows) == len(cases) * repeats,
        'rows': rows,
        'expected': len(cases) * repeats,
    }


def recover_case(events: list[dict], case_id: str, repeat: int) -> dict:
    rows = {}
    for event in events:
        row = event.get('row')
        if (
            isinstance(row, dict)
            and event.get('case_id', row.get('case_id')) == case_id
            and event.get('repeat', row.get('repeat')) == repeat
        ):
            rows[(case_id, repeat)] = row
    if (case_id, repeat) in rows:
        return rows[(case_id, repeat)]
    return {
        'case_id': case_id,
        'repeat': repeat,
        'transport_status': 'not_started',
        'run_id': None,
    }

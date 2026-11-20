#!/usr/bin/env python3
"""Run a resumable dev baseline against real public Agent APIs."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

import httpx

import common  # noqa: F401
from common import ROOT, load_yaml, resolve, token


async def run(args) -> dict:
    import subprocess
    from workbench_agent.evaluation.api_client import EvaluationAPI
    from workbench_agent.evaluation.collector import collect_case, read_rows, write_rows
    from workbench_agent.evaluation.contracts import hash_json, validate_dataset
    from workbench_agent.evaluation.journal import Journal, atomic_json

    config = load_yaml(args.config)
    dataset_path = resolve(args.dataset or config['dataset_path'])
    dataset = json.loads(dataset_path.read_text(encoding='utf-8'))
    validate_dataset(dataset)
    if not dataset['cases']:
        raise ValueError('dataset contains no business cases; refusing to create a baseline')
    experiment_id = args.experiment_id
    if not experiment_id or experiment_id != experiment_id.strip():
        raise ValueError('experiment id is required')
    config_for_hash = {
        key: value for key, value in config.items() if key not in {'token_env'}
    }
    config_hash = hash_json(config_for_hash)
    dataset_hash = hash_json(dataset)
    output = resolve(config['output_root']) / experiment_id
    manifest_path = output / 'experiment.json'
    journal = Journal(output / 'journal.jsonl')
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        if (
            manifest.get('experiment_id') != experiment_id
            or manifest.get('dataset_hash') != dataset_hash
            or manifest.get('config_hash') != config_hash
        ):
            raise ValueError('experiment inputs changed; choose a new experiment id')
    else:
        try:
            git_commit = subprocess.check_output(
                ['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True
            ).strip()
        except (OSError, subprocess.CalledProcessError):
            git_commit = None
        manifest = {
            'schema_version': 'eval-experiment-v1',
            'experiment_id': experiment_id,
            'dataset_hash': dataset_hash,
            'config_hash': config_hash,
            'seed_manifest_hash': hash_json(
                load_yaml(str(resolve(config['seed_manifest_path'])))
            ),
            'git_commit': git_commit,
            'graph_id': config['expected_graph_id'],
            'provenance': None,
            'collector_version': 'knowledge-collector-v1',
            'started_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            'budget': {
                'limits': config['limits'],
                'pricing': config['pricing'],
                'experiment_budget_micros': config['experiment_budget_micros'],
            },
        }
        atomic_json(manifest_path, manifest)
    rows = read_rows(output / 'results.jsonl')
    by_request = {row['request_key']: row for row in rows}
    repeat_count = args.repeats
    async with httpx.AsyncClient(
        base_url=config['api_base_url'].rstrip('/'),
        timeout=config['request_timeout_seconds'],
        follow_redirects=False,
    ) as client:
        api = EvaluationAPI(client, token=token(config))
        experiment = {**manifest, 'config': config}
        for case in dataset['cases']:
            for repeat in range(repeat_count):
                deadline = time.monotonic() + config['case_timeout_seconds']
                row = await collect_case(
                    api,
                    case,
                    agent_id=config['agent_id'],
                    experiment=experiment,
                    repeat=repeat,
                    journal=journal,
                    deadline=deadline,
                )
                by_request[row['request_key']] = row
                write_rows(output / 'results.jsonl', list(by_request.values()))
                manifest['provenance'] = experiment.get('provenance')
                atomic_json(manifest_path, manifest)
    return {
        'experiment_id': experiment_id,
        'rows': len(by_request),
        'completed': sum(
            row.get('transport_status') == 'completed'
            for row in by_request.values()
        ),
        'needs_reconciliation': sum(
            row.get('transport_status') != 'completed'
            for row in by_request.values()
        ),
        'output': str(output),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='config/evaluation/local.yaml')
    parser.add_argument('--dataset')
    parser.add_argument('--experiment-id', required=True)
    parser.add_argument('--repeats', type=int, default=1)
    args = parser.parse_args()
    if args.repeats < 1 or args.repeats > 5:
        parser.error('--repeats must be between 1 and 5')
    try:
        result = asyncio.run(run(args))
    except (ValueError, OSError, httpx.HTTPError) as error:
        print(json.dumps({'ready': False, 'error': type(error).__name__, 'message': str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result['needs_reconciliation'] == 0 else 2


if __name__ == '__main__':
    raise SystemExit(main())

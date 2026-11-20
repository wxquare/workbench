#!/usr/bin/env python3
"""Read-only local baseline preflight."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import common  # noqa: F401
from common import ROOT, load_yaml, resolve  # noqa: F401


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--config', default='config/evaluation/local.yaml'
    )
    parser.add_argument(
        '--agent-settings',
        help='Load Agent config for deployment checks; no network or secrets are resolved.',
    )
    args = parser.parse_args()
    from workbench_agent.evaluation.contracts import validate_experiment_config
    from workbench_agent.evaluation.preflight import preflight
    from workbench_agent.settings import load_settings

    config = load_yaml(args.config)
    resolved = {
        **config,
        'agent_config_path': str(resolve(config['agent_config_path'])),
        'mcp_config_path': str(resolve(config['mcp_config_path'])),
        'dataset_path': str(resolve(config['dataset_path'])),
        'seed_manifest_path': str(resolve(config['seed_manifest_path'])),
        'output_root': str(resolve(config['output_root'])),
    }
    settings = None
    if args.agent_settings:
        os.environ.setdefault('WORKBENCH_ASSET_ROOT', str(resolve('.')))
        os.environ.setdefault(
            'WORKBENCH_DATABASE_URL',
            'sqlite+aiosqlite:////tmp/workbench-preflight.sqlite3',
        )
        settings = load_settings(args.agent_settings)
    try:
        validate_experiment_config(resolved)
        result = preflight(resolved, environ=os.environ, settings=settings)
    except (ValueError, TypeError) as error:
        result = {
            'ready': False,
            'checks': [{
                'ok': False,
                'code': 'SESSION_CONFIG',
                'message': str(error),
            }],
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result['ready'] else 1


if __name__ == '__main__':
    raise SystemExit(main())

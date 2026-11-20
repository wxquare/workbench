#!/usr/bin/env python3
"""Publish real seed knowledge through public, authenticated APIs."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import httpx

import common  # noqa: F401
from common import ROOT, load_yaml, resolve, token  # noqa: F401


async def run(args) -> dict:
    from workbench_agent.evaluation.api_client import EvaluationAPI
    from workbench_agent.evaluation.importer import import_seed

    config = load_yaml(args.config)
    manifest_path = Path(args.manifest or config['seed_manifest_path'])
    manifest = load_yaml(str(resolve(str(manifest_path))))
    timeout = config['request_timeout_seconds']
    state_path = resolve(config['output_root']) / config['seed_namespace'] / 'import-state.json'
    async with httpx.AsyncClient(
        base_url=config['api_base_url'].rstrip('/'),
        timeout=timeout,
        follow_redirects=False,
    ) as client:
        api = EvaluationAPI(client, token=token(config))
        return await import_seed(
            api,
            manifest,
            namespace=config['seed_namespace'],
            state_path=state_path,
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='config/evaluation/local.yaml')
    parser.add_argument('--manifest')
    args = parser.parse_args()
    try:
        result = asyncio.run(run(args))
    except (ValueError, OSError, httpx.HTTPError) as error:
        print(json.dumps({'ready': False, 'error': type(error).__name__, 'message': str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

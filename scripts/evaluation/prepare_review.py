#!/usr/bin/env python3
"""Create a review file without inventing semantic labels."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import common  # noqa: F401
from common import ROOT, load_yaml, resolve  # noqa: F401


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default='config/evaluation/local.yaml')
    parser.add_argument('--experiment-id', required=True)
    args = parser.parse_args()
    from workbench_agent.evaluation.collector import prepare_review, read_rows
    from workbench_agent.evaluation.contracts import validate_dataset
    from workbench_agent.evaluation.journal import atomic_json

    config = load_yaml(args.config)
    dataset = load_yaml(str(resolve(config['dataset_path'])))
    validate_dataset(dataset)
    output = resolve(config['output_root']) / args.experiment_id
    review_path = output / 'review.json'
    previous = (
        json.loads(review_path.read_text(encoding='utf-8'))
        if review_path.exists()
        else None
    )
    review = prepare_review(
        dataset,
        read_rows(output / 'results.jsonl'),
        previous=previous,
    )
    atomic_json(review_path, review)
    print(json.dumps({'cases': len(review['cases']), 'output': str(review_path)}, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

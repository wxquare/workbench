"""Versioned knowledge evaluation command surface."""

from __future__ import annotations

import json

from workbench_agent.evaluation.dataset import dataset_fingerprint, load_dataset


def main(args) -> int:
    if args.eval_command == "validate":
        dataset = load_dataset(args.dataset)
        print(
            json.dumps(
                {
                    "valid": True,
                    "dataset_hash": dataset_fingerprint(dataset),
                    "cases": len(dataset["cases"]),
                },
                sort_keys=True,
            )
        )
        return 0
    raise ValueError("unsupported eval command")

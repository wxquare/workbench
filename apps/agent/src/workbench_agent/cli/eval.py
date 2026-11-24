"""Versioned knowledge evaluation command surface."""

from __future__ import annotations

import json
from pathlib import Path

from workbench_agent.evaluation.comparison import compare_reports
from workbench_agent.evaluation.dataset import dataset_fingerprint, load_dataset
from workbench_agent.evaluation.dataset import select_cases
from workbench_agent.evaluation.journal import atomic_json
from workbench_agent.evaluation.metrics import summarize_repeats
from workbench_agent.evaluation.metrics import apply_dataset_boundary
from workbench_agent.evaluation.runner import (
    ExperimentDirectory,
    _config_hash,
    _manifest,
    run_experiment,
)
import asyncio
import hashlib
import os
from workbench_agent.evaluation.api_client import EvaluationAPI
from workbench_agent.evaluation.journal import Journal
from workbench_agent.evaluation.scoring import score_result
from workbench_agent.evaluation.tool_conversation_eval import (
    load_tool_dataset,
    score_tool_result,
    tool_dataset_fingerprint,
)
from workbench_agent.evaluation.tool_runner import run_tool_experiment


def _tool_dataset(path):
    if path is None:
        return False
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError, UnicodeError):
        return False
    return isinstance(value, dict) and value.get('schema_version') == 'tool-conversation-eval-v1'


def main(args) -> int:
    if args.eval_command == 'validate':
        tool = _tool_dataset(args.dataset)
        purpose = getattr(args, 'purpose', 'business')
        dataset = (
            load_tool_dataset(args.dataset) if tool else load_dataset(args.dataset, purpose=purpose)
        )
        print(
            json.dumps(
                {
                    'valid': True,
                    'schema_version': dataset['schema_version'],
                    'dataset_hash': (
                        tool_dataset_fingerprint(dataset)
                        if tool
                        else dataset_fingerprint(dataset, purpose=purpose)
                    ),
                    'cases': len(dataset['cases']),
                },
                sort_keys=True,
            )
        )
        return 0
    if args.eval_command == 'run':
        purpose = (
            'business'
            if getattr(args, 'purpose', None) == 'release'
            else getattr(args, 'purpose', None) or 'business'
        )
        manifest_path = Path(args.resume) / 'manifest.json' if args.resume else None
        resumed_tool = (
            manifest_path is not None
            and manifest_path.exists()
            and json.loads(manifest_path.read_text())
            .get('dataset_distribution', {})
            .get('schema_version')
            == 'tool-conversation-eval-v1'
        )
        tool = _tool_dataset(args.dataset) or resumed_tool
        if bool(args.resume) == bool(args.dataset and args.config and args.experiment_id):
            raise ValueError('provide either new inputs or --resume')
        if args.resume:
            experiment = Path(args.resume)
            manifest = json.loads((experiment / 'experiment.json').read_text())
            config_path = args.config or manifest.get('config_path')
            dataset = (
                load_tool_dataset(args.dataset or manifest.get('dataset_path'))
                if tool
                else load_dataset(args.dataset or manifest.get('dataset_path'), purpose=purpose)
            )
            config = json.loads(Path(config_path).read_text()) if config_path else None
            split, repeats = manifest['split'], manifest['repeats']
            if config is None:
                raise ValueError('resume requires the original config path')
        else:
            dataset_path, config_path = Path(args.dataset), Path(args.config)
            dataset = (
                load_tool_dataset(dataset_path)
                if tool
                else load_dataset(dataset_path, purpose=purpose)
            )
            config = json.loads(config_path.read_text())
            experiment = (
                Path(config.get('output_root', 'artifacts/evaluation')) / args.experiment_id
            )
            split, repeats = args.split, args.repeats
            manifest = _manifest(
                dataset,
                config,
                experiment_id=args.experiment_id,
                split=split,
                repeats=repeats,
                git_commit=None,
                comparability='available',
            )
            manifest['dataset_path'] = str(dataset_path)
            manifest['config_path'] = str(config_path)
            manifest['purpose'] = purpose
            manifest['dataset_kind'] = dataset['dataset_kind']
            if tool:
                manifest['scoring_version'] = 'tool-conversation-rules-v1'
        if args.split == 'holdout' and args.purpose != 'release':
            raise ValueError('holdout may only run for an explicit release')
        with ExperimentDirectory(experiment):
            if not (experiment / 'manifest.json').exists():
                atomic_json(experiment / 'manifest.json', manifest)
            journal = Journal(experiment / 'journal.jsonl')
            token = os.environ.get(config['token_env']) if config else None
            if not token:
                raise ValueError('evaluation token is not set')
            import httpx

            async def execute():
                async with httpx.AsyncClient(
                    base_url=config['api_base_url'],
                    timeout=config['request_timeout_seconds'],
                ) as client:
                    runner = run_tool_experiment if tool else run_experiment
                    return await runner(
                        dataset,
                        config,
                        manifest=manifest,
                        journal=journal,
                        api=EvaluationAPI(client, token=token),
                        split=split,
                        repeats=repeats,
                        deadline_seconds=config['case_timeout_seconds'],
                    )

            result = asyncio.run(execute())
        if result['rows']:
            manifest = json.loads((experiment / 'manifest.json').read_text())
            for row in result['rows']:
                if row.get('evidence'):
                    manifest.setdefault('provenance', row['evidence'])
                    break
            atomic_json(experiment / 'manifest.json', manifest)
        report = {
            'manifest': manifest,
            'complete': result['complete'],
            'rows': len(result['rows']),
            'expected': result['expected'],
        }
        report = apply_dataset_boundary(report, dataset_kind=dataset['dataset_kind'])
        report['purpose'] = purpose
        atomic_json(experiment / 'run-summary.json', report)
        return 0 if result['complete'] else 1
    if args.eval_command == 'score':
        purpose = getattr(args, 'purpose', 'business')
        experiment = Path(args.experiment)
        tool = _tool_dataset(args.dataset)
        dataset = (
            load_tool_dataset(args.dataset) if tool else load_dataset(args.dataset, purpose=purpose)
        )
        results = [
            json.loads(line)
            for line in (experiment / 'results.jsonl').read_text().splitlines()
            if line
        ]
        expected_cases = (
            [case for case in dataset['cases'] if case.get('split', 'dev') == args.split]
            if tool
            else select_cases(dataset, args.split, purpose=purpose)
        )
        scores = [
            (score_tool_result(case, row) if tool else score_result(case, row))
            for case in expected_cases
            for row in results
            if row.get('case_id') == case['id'] and row.get('repeat') in range(args.repeats)
        ]
        # Include missing assignments explicitly through their expected case
        # identifiers; metrics will report rather than drop them.
        k = args.repeats
        report = {
            'schema_version': 'eval-report-v1',
            'dataset_hash': (
                tool_dataset_fingerprint(dataset)
                if tool
                else dataset_fingerprint(dataset, purpose=purpose)
            ),
            'split': args.split,
            'repeats': k,
            'scoring_version': ('tool-conversation-rules-v1' if tool else 'knowledge-rules-v1'),
            'metrics': summarize_repeats(
                scores,
                expected_cases=[case['id'] for case in expected_cases],
                k=k,
            ),
            'quality_status': 'unavailable',
        }
        report['purpose'] = purpose
        report = apply_dataset_boundary(report, dataset_kind=dataset['dataset_kind'])
        atomic_json(Path(args.output), report)
        return (
            0
            if report['metrics']['complete']
            and not report['metrics']['unauthorized_accesses']
            and not report['metrics']['incorrect_conclusions']
            else 1
        )
    if args.eval_command == 'compare':
        baseline = json.loads((Path(args.baseline) / 'report.json').read_text())
        candidate = json.loads((Path(args.candidate) / 'report.json').read_text())
        policy = json.loads(Path(args.policy).read_text())
        result = compare_reports(baseline, candidate, policy)
        atomic_json(Path(args.output), result)
        return 0 if result['status'] == 'passed' else 1
    raise ValueError('unsupported eval command')

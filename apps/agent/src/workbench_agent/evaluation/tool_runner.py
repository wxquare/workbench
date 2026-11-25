"""Resumable tool-conversation collection without knowledge claim semantics."""

from __future__ import annotations

import time
from pathlib import Path

from workbench_agent.evaluation.collector import (
    collect_case,
    read_rows,
    write_rows,
)
from workbench_agent.evaluation.budget import ExperimentBudget
from workbench_agent.evaluation.contracts import make_run_key
from workbench_agent.evaluation.runner import _config_hash
from workbench_agent.evaluation.tool_conversation_eval import (
    tool_dataset_fingerprint,
)


def _selected(dataset, split):
    return [case for case in dataset['cases'] if case['split'] == split]


async def run_tool_experiment(
    dataset,
    config,
    *,
    manifest,
    journal,
    api,
    split,
    repeats,
    deadline_seconds,
):
    if config.get('expected_graph_id') != 'conversation.tool_use':
        raise ValueError('tool runner requires conversation.tool_use')
    if split != 'dev':
        raise ValueError('tool conversation experiments only allow dev split')
    if repeats != 1:
        raise ValueError('tool conversation runner requires one repeat')
    expected_hash = tool_dataset_fingerprint(dataset)
    if manifest.get('dataset_hash') != expected_hash:
        raise ValueError('tool dataset does not match manifest')
    if manifest.get('config_hash') != _config_hash(config):
        raise ValueError('config does not match manifest')
    if any(case['review_status'] != 'reviewed' for case in dataset['cases']):
        raise ValueError('tool dataset must be human reviewed')
    cases = _selected(dataset, split)
    budget = ExperimentBudget(config, journal)
    output = Path(journal.path).with_name('results.jsonl')
    rows = {(row.get('case_id'), row.get('repeat')): row for row in read_rows(output)}
    for event in journal.events():
        row = event.get('row')
        if isinstance(row, dict) and row.get('case_id') and row.get('repeat') is not None:
            rows[(row['case_id'], int(row['repeat']))] = row
    for case in cases:
        repeat = 0
        recovered = rows.get((case['id'], repeat))
        if recovered:
            budget.observe(recovered)
            if await budget.refresh_terminal(recovered, api):
                write_rows(output, list(rows.values()))
        if (
            recovered
            and recovered.get('run_id')
            and recovered.get('run_status')
            in {'succeeded', 'partial', 'failed', 'cancelled', 'timed_out', 'expired'}
        ):
            continue
        if recovered and recovered.get('transport_status') == 'rejected':
            continue
        key = make_run_key(
            manifest['experiment_id'],
            manifest['dataset_hash'],
            manifest['config_hash'],
            case['id'],
            repeat,
        )
        already_sent = recovered and (
            recovered.get('run_id')
            or recovered.get('transport_status') in ('submitted', 'needs_reconciliation')
        )
        if not already_sent and not budget.reserve(case['id'], repeat, key):
            continue
        existing = rows.get((case['id'], repeat))
        if (
            existing
            and existing.get('transport_status')
            in {'needs_reconciliation', 'interrupted', 'timeout', 'accepted', 'submitted'}
            and existing.get('run_id')
        ):
            # Never issue a second create for an accepted Run.
            experiment = {**manifest, 'config': config}
            row = await collect_case(
                api,
                case,
                agent_id=config['agent_id'],
                experiment=experiment,
                repeat=repeat,
                journal=journal,
                deadline=time.monotonic() + deadline_seconds,
            )
        elif existing and existing.get('transport_status') == 'needs_reconciliation':
            row = existing
        else:
            row = await collect_case(
                api,
                case,
                agent_id=config['agent_id'],
                experiment={**manifest, 'config': config},
                repeat=repeat,
                journal=journal,
                deadline=time.monotonic() + deadline_seconds,
            )
        rows[(case['id'], repeat)] = row
        budget.observe(row)
        write_rows(output, list(rows.values()))
    values = list(rows.values())
    return {
        'complete': (
            len(values) == len(cases)
            and all(row.get('transport_status') in {'completed', 'rejected'} for row in values)
        ),
        'rows': values,
        'expected': len(cases),
    }

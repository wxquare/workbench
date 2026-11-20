"""Collect complete Run evidence without treating partial transport as success."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import httpx

from workbench_agent.evaluation.api_client import APIError, EvaluationAPI
from workbench_agent.evaluation.contracts import (
    canonical,
    hash_json,
    make_run_key,
    validate_dataset,
)
from workbench_agent.evaluation.journal import fsync_directory


TERMINAL = {'succeeded', 'partial', 'failed', 'cancelled', 'timed_out', 'expired'}
INTERRUPTED = {'waiting_input', 'waiting_approval'}


def _event_rows(journal, request_key: str) -> list[dict]:
    return [
        event
        for event in journal.events()
        if event.get('request_key') == request_key
    ]


def _validated_version_ref(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get('resource_id'), str)
        and isinstance(value.get('version'), str)
        and isinstance(value.get('content_hash'), str)
    )


def verify_provenance(provenance: dict, config: dict) -> None:
    if not isinstance(provenance, dict):
        raise ValueError('provenance missing')
    if provenance.get('graph_id') != config['expected_graph_id']:
        raise ValueError('graph version mismatch')
    if not _validated_version_ref(provenance.get('agent_profile')):
        raise ValueError('agent profile version missing')
    if not _validated_version_ref(provenance.get('default_model')):
        raise ValueError('default model version missing')
    expected_models = config.get('expected_model_refs') or []
    present = {
        canonical(provenance['default_model']),
        *(canonical(item['ref']) for item in provenance.get('role_models', [])),
    }
    missing = [ref for ref in expected_models if canonical(ref) not in present]
    if missing:
        raise ValueError('configured model version mismatch')
    expected_profile = config.get('expected_profile_ref')
    if expected_profile is not None and provenance['agent_profile'] != expected_profile:
        raise ValueError('configured profile version mismatch')


def _empty_row(case: dict, experiment: dict, repeat: int, request_key: str) -> dict:
    return {
        'schema_version': 'eval-result-v1',
        'experiment_id': experiment['experiment_id'],
        'case_id': case['id'],
        'repeat': repeat,
        'request_key': request_key,
        'run_id': None,
        'transport_status': 'not_started',
        'run_status': None,
        'outcome': None,
        'evidence': [],
        'usage': None,
        'latency_ms': None,
        'error_code': None,
    }


def read_rows(path: Path) -> list[dict]:
    import json
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding='utf-8').splitlines():
        if line:
            value = json.loads(line)
            if not isinstance(value, dict):
                raise ValueError('result row must be an object')
            rows.append(value)
    return rows


def write_rows(path: Path, rows: list[dict]) -> None:
    # Each line is its own immutable observation; replacing preserves a valid
    # complete snapshot after every successful case.
    content = ''.join(canonical(row) + '\n' for row in rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'.{path.name}.tmp')
    with temporary.open('w', encoding='utf-8') as handle:
        handle.write(content)
        handle.flush()
        import os
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    fsync_directory(path.parent)


async def collect_case(
    api: EvaluationAPI,
    case: dict,
    *,
    agent_id: str,
    experiment: dict,
    repeat: int,
    journal,
    deadline: float,
) -> dict:
    config = experiment['config']
    request_key = make_run_key(
        experiment['experiment_id'],
        experiment['dataset_hash'],
        experiment['config_hash'],
        case['id'],
        repeat,
    )
    events = _event_rows(journal, request_key)
    row = next(
        (
            event['row']
            for event in reversed(events)
            if isinstance(event.get('row'), dict)
        ),
        _empty_row(case, experiment, repeat, request_key),
    )
    run_id = row.get('run_id')
    if row.get('transport_status') == 'needs_reconciliation':
        return row
    if not run_id:
        if row.get('transport_status') == 'submitted':
            row['transport_status'] = 'needs_reconciliation'
            row['error_code'] = 'ACCEPTANCE_UNKNOWN'
            journal.append(
                {
                    'event': 'case_result',
                    'request_key': request_key,
                    'row': row,
                }
            )
            return row
        body = {
            'agent_id': agent_id,
            'mode': 'qa',
            'input': {'type': 'text', 'text': case['question'], 'artifact_ids': []},
            'business_context': {'query_scope': case['query_scope']},
            'client_message_id': request_key,
            'budgets': config['limits'],
        }
        row['transport_status'] = 'submitted'
        journal.append(
            {
                'event': 'create_sent',
                'request_key': request_key,
                'request_body_hash': hash_json(body),
            }
        )
        try:
            accepted = await api.create_run(body, key=request_key)
        except APIError as error:
            if error.status in (400, 401, 403, 404, 409, 422):
                row.update(
                    transport_status='rejected',
                    error_code=error.code,
                )
                journal.append(
                    {
                        'event': 'create_response',
                        'request_key': request_key,
                        'status': error.status,
                        'error_code': error.code,
                        'row': row,
                    }
                )
                return row
            # The server may have committed before transport was lost.
            row.update(
                transport_status='needs_reconciliation',
                error_code=error.code,
            )
            journal.append(
                {
                    'event': 'create_uncertain',
                    'request_key': request_key,
                    'error_code': error.code,
                    'row': row,
                }
            )
            return row
        except (asyncio.TimeoutError, httpx.TransportError) as error:
            # The request may have reached the server. The durable journal keeps
            # the original key and forbids an automatic second POST.
            row.update(
                transport_status='needs_reconciliation',
                error_code='ACCEPTANCE_UNKNOWN',
            )
            journal.append(
                {
                    'event': 'create_uncertain',
                    'request_key': request_key,
                    'error_code': type(error).__name__,
                    'row': row,
                }
            )
            return row
        run_id = accepted.get('run_id')
        if not run_id:
            row.update(
                transport_status='needs_reconciliation',
                error_code='ACCEPTANCE_INVALID',
            )
            journal.append(
                {
                    'event': 'create_invalid',
                    'request_key': request_key,
                    'row': row,
                }
            )
            return row
        row.update(
            run_id=run_id,
            transport_status='accepted',
        )
        journal.append(
            {
                'event': 'create_response',
                'request_key': request_key,
                'status': 202,
                'row': row,
            }
        )

    try:
        provenance = await api.provenance(run_id)
        verify_provenance(provenance, config)
        if (
            experiment.get('provenance') is not None
            and experiment['provenance'] != provenance
        ):
            raise ValueError('provenance changed within experiment')
        experiment['provenance'] = provenance
    except (APIError, ValueError) as error:
        code = getattr(error, 'code', 'PROVENANCE_MISMATCH')
        row.update(
            transport_status='needs_reconciliation',
            error_code=code,
        )
        journal.append(
            {
                'event': 'provenance_error',
                'request_key': request_key,
                'error_code': code,
                'row': row,
            }
        )
        return row

    usage = None
    try:
        usage = await api.usage(run_id)
        row['usage'] = usage
        budget = config.get('experiment_budget_micros')
        amount = usage.get('amount_micros')
        prices = config.get('pricing') or {}
        if (
            budget is not None
            and prices.get('currency') is not None
            and usage.get('currency') not in (None, prices['currency'])
        ):
            raise ValueError('experiment currency does not match usage currency')
        if budget is not None and type(amount) is int and amount > budget:
            row.update(
                transport_status='needs_reconciliation',
                error_code='EXPERIMENT_BUDGET_EXCEEDED',
            )
            journal.append(
                {
                    'event': 'budget_stop',
                    'request_key': request_key,
                    'amount_micros': amount,
                    'budget_micros': budget,
                    'row': row,
                }
            )
            return row
    except ValueError as error:
        row.update(
            transport_status='needs_reconciliation',
            error_code='COST_CURRENCY_MISMATCH',
        )
        journal.append(
            {
                'event': 'budget_stop',
                'request_key': request_key,
                'error_code': str(error),
                'row': row,
            }
        )
        return row
    except APIError as error:
        row['error_code'] = 'USAGE_UNAVAILABLE'
        journal.append(
            {
                'event': 'usage_error',
                'request_key': request_key,
                'error_code': error.code,
            }
        )

    start = time.monotonic()
    while time.monotonic() < deadline:
        try:
            run = await api.get_run(run_id)
        except APIError as error:
            if error.status in (401, 403, 404):
                row.update(
                    transport_status='needs_reconciliation',
                    error_code=error.code,
                )
                journal.append(
                    {
                        'event': 'run_error',
                        'request_key': request_key,
                        'error_code': error.code,
                        'row': row,
                    }
                )
                return row
            await asyncio.sleep(0.25)
            continue
        status = run.get('status')
        row['run_status'] = status
        if status in TERMINAL:
            row.update(
                outcome=run.get('outcome'),
                transport_status='completed',
                latency_ms=int((time.monotonic() - start) * 1000),
            )
            break
        if status in INTERRUPTED:
            row.update(
                transport_status='interrupted',
                outcome=run.get('outcome'),
                latency_ms=int((time.monotonic() - start) * 1000),
            )
            journal.append(
                {
                    'event': 'case_result',
                    'request_key': request_key,
                    'row': row,
                }
            )
            return row
        await asyncio.sleep(0.25)
    else:
        row.update(
            transport_status='timeout',
            error_code='CASE_DEADLINE_EXCEEDED',
            latency_ms=int((time.monotonic() - start) * 1000),
        )
        journal.append(
            {
                'event': 'case_result',
                'request_key': request_key,
                'row': row,
            }
        )
        return row

    refs = []
    outcome = row.get('outcome') or {}
    for ref in outcome.get('evidence_refs', []) if isinstance(outcome, dict) else []:
        if isinstance(ref, str) and ref not in refs:
            refs.append(ref)
    for ref in refs:
        try:
            evidence = await api.evidence(run_id, ref)
        except APIError as error:
            if error.status in (410, 404):
                row['error_code'] = 'EVIDENCE_UNAVAILABLE'
                break
            raise
        row['evidence'].append({'evidence_id': ref, 'content': evidence})
    if usage is None:
        try:
            row['usage'] = await api.usage(run_id)
        except APIError:
            row['error_code'] = 'USAGE_UNAVAILABLE'
    journal.append(
        {
            'event': 'case_result',
            'request_key': request_key,
            'row': row,
        }
    )
    return row


def prepare_review(
    dataset: dict, rows: list[dict], *, previous: dict | None = None
) -> dict:
    """Copy labels and prefill only mechanical output fields."""
    validate_dataset(dataset)
    by_id: dict[str, list[dict]] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('case_id'), str):
            raise ValueError('invalid result row')
        by_id.setdefault(row['case_id'], []).append(row)
    result = {
        'schema_version': 'knowledge-eval-v1',
        'dataset_kind': 'business',
        'cases': [],
    }
    previous_cases = {
        case['id']: case
        for case in (previous or {}).get('cases', [])
        if isinstance(case, dict) and isinstance(case.get('id'), str)
    }
    for case in dataset['cases']:
        copies = by_id.get(case['id'], [])
        selected = next(
            (row for row in copies if row.get('run_status') in TERMINAL),
            copies[0] if copies else {},
        )
        run_id = selected.get('run_id')
        outcome = selected.get('outcome') or {}
        claims = []
        knowledge = outcome.get('knowledge_answer') if isinstance(outcome, dict) else None
        for claim in (knowledge or {}).get('claims', []):
            citations = claim.get('citations')
            if not isinstance(citations, list) or not citations:
                continue
            claims.append(
                {
                    'text': claim.get('text'),
                    'cited': True,
                    'citation_correct': None,
                    'citations': citations,
                }
            )
        copied = {
            **case,
            'result_hash': hash_json(
                {
                    key: value
                    for key, value in selected.items()
                    if key in {'run_id', 'request_key', 'run_status', 'outcome', 'usage'}
                }
            ),
            'result_review': {
                'reviewer': None,
                'output_present': bool(run_id and outcome),
                'output_ref': run_id,
                'claims': claims,
                'answer_usable': None,
                'unauthorized_access': None,
                'degradation_reason': None,
                'review_status': 'pending',
            },
        }
        old = previous_cases.get(case['id'])
        if old and old.get('result_review', {}).get('output_ref') == run_id:
            row_set = {
                key: value
                for key, value in selected.items()
                if key in {'run_id', 'request_key', 'run_status', 'outcome', 'usage'}
            }
            old_review = old['result_review']
            if old.get('result_hash') != hash_json(row_set):
                old_review = {
                    **old_review,
                    'review_status': 'stale',
                    'stale_reason': 'result_changed',
                }
            copied['result_review'] = old_review
            copied['review_status'] = old.get('review_status', case.get('review_status'))
            copied['reviewer'] = old.get('reviewer', case.get('reviewer'))
        result['cases'].append(copied)
    return result

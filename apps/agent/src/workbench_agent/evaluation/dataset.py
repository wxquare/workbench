"""Versioned knowledge datasets and experiment manifest contracts."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from workbench_agent.domain.query_scope import validate_qa_scope
SCENARIOS = {
    'answerable',
    'multiple_sources',
    'no_answer',
    'conflict',
    'stale',
    'unauthorized',
}


def canonical(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


SPLITS = {'dev', 'holdout'}
DATASET_FIELDS = {
    'id',
    'question',
    'query_scope',
    'scenario',
    'answerable',
    'expected_source_ids',
    'must_include',
    'review_status',
    'reviewer',
    'split',
    'checks',
    'notes',
    'result_review',
    'result_hash',
}
REQUIRED_CASE_FIELDS = {
    'id',
    'question',
    'query_scope',
    'scenario',
    'answerable',
    'expected_source_ids',
    'must_include',
}
MAX_PATTERN_LENGTH = 256
MAX_MATCH_TEXT_LENGTH = 65_536
MAX_DATASET_BYTES = 8 * 1024 * 1024


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value == value.strip()


def _normalized_scope(scope: dict) -> dict:
    result = validate_qa_scope(scope)
    for field in ('start_at', 'end_at'):
        value = result[field]
        try:
            parsed = datetime.strptime(value, '%Y-%m-%d %H:%M:%S').replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        result[field] = parsed.astimezone(timezone.utc).isoformat()
    return result


def safe_regex(value: str) -> re.Pattern:
    """Accept a small linear-ish subset and reject constructs prone to traps."""
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > MAX_PATTERN_LENGTH
    ):
        raise ValueError('invalid regex length')
    forbidden = (
        '\\1',
        '\\2',
        '\\3',
        '\\4',
        '\\5',
        '\\6',
        '\\7',
        '\\8',
        '\\9',
        '(?=',
        '(?!',
        '(?<=',
        '(?<!',
        '(?>',
        '(?P=',
        '(?(',
        '(?#',
    )
    if any(token in value for token in forbidden):
        raise ValueError('unsafe regex construct')
    quantifier = re.compile(r'(?:[*+?]|\{\d+(?:,\d*)?\})')
    count = len(quantifier.findall(value))
    if count > 1:
        raise ValueError('nested or repeated quantifier')
    try:
        return re.compile(value)
    except re.error:
        raise ValueError('invalid regex') from None


def _definition(case: dict) -> dict:
    scope = _normalized_scope(case['query_scope'])
    return {
        'id': case['id'],
        'question': ' '.join(case['question'].split()).casefold(),
        'query_scope': scope,
        'scenario': case['scenario'],
        'answerable': case['answerable'],
        'expected_source_ids': sorted(set(case['expected_source_ids'])),
        'must_include': list(case['must_include']),
        'checks': case.get('checks', []),
        'split': case.get('split', 'dev'),
    }


def validate_dataset(value: dict, *, require_complete: bool = False) -> dict:
    """Validate schema, unique cases, split coverage and safe checks."""
    if not isinstance(value, dict):
        raise ValueError('dataset must be an object')
    if value.get('schema_version') != 'knowledge-eval-v1':
        raise ValueError('invalid dataset schema')
    if value.get('dataset_kind') != 'business':
        raise ValueError('baseline datasets must be declared as business')
    if set(value) - {'schema_version', 'dataset_kind', 'cases', 'access'}:
        raise ValueError('unexpected dataset fields')
    cases = value.get('cases')
    if not isinstance(cases, list):
        raise ValueError('dataset cases must be a list')
    identifiers: set[str] = set()
    counts = {'dev': 0, 'holdout': 0}
    scenarios = {'dev': set(), 'holdout': set()}
    key_records: list[dict] = []
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError('each case must be an object')
        if set(case) - DATASET_FIELDS:
            raise ValueError('unexpected case fields')
        case_id, question = case.get('id'), case.get('question')
        if not _text(case_id) or len(case_id) > 128:
            raise ValueError('invalid case id')
        if case_id in identifiers:
            raise ValueError('duplicate case id')
        identifiers.add(case_id)
        if (
            (require_complete and not isinstance(question, str))
            or (question is not None and not isinstance(question, str))
            or (require_complete and not question.strip())
            or (isinstance(question, str) and len(question) > 4000)
        ):
            raise ValueError('invalid question')
        missing = {
            field
            for field in REQUIRED_CASE_FIELDS
            if field not in case
            or (
                field in {'expected_source_ids', 'must_include'}
                and not isinstance(case[field], list)
            )
        }
        if missing and require_complete:
            raise ValueError('missing case fields: ' + ', '.join(sorted(missing)))
        if not require_complete:
            if case.get('scenario') is not None and case['scenario'] not in SCENARIOS:
                raise ValueError('unsupported scenario')
            definition = {
                'question': ' '.join((question or '').split()).casefold(),
                'query_scope': (
                    _normalized_scope(case.get('query_scope'))
                    if 'query_scope' in case
                    else None
                ),
            }
            if any(
                item['question'] == definition['question']
                and item['query_scope'] == definition['query_scope']
                for item in key_records
            ):
                raise ValueError('duplicate question and scope')
            key_records.append(definition)
            counts[case.get('split', 'dev')] = counts.get(
                case.get('split', 'dev'), 0
            ) + 1
            continue
        scenario = case.get('scenario')
        if scenario not in SCENARIOS:
            raise ValueError('unsupported scenario')
        if type(case.get('answerable')) is not bool:
            raise ValueError('answerable must be boolean')
        if (scenario == 'answerable' and case['answerable'] is not True) or (
            scenario == 'no_answer' and case['answerable'] is not False
        ):
            raise ValueError('scenario and answerable label disagree')
        try:
            _normalized_scope(case['query_scope'])
        except (ValueError, TypeError):
            raise ValueError('invalid query scope') from None
        for field in ('expected_source_ids', 'must_include'):
            labels = case[field]
            if (
                not isinstance(labels, list)
                or any(not _text(item) for item in labels)
                or len(labels) != len(set(labels))
            ):
                raise ValueError(f'{field} must contain unique nonempty strings')
        if case['answerable'] and (
            not case['expected_source_ids'] or not case['must_include']
        ):
            raise ValueError('answerable cases require sources and required facts')
        split = case.get('split', 'dev')
        if split not in SPLITS:
            raise ValueError('unsupported split')
        checks = case.get('checks', [])
        if not isinstance(checks, list) or len(checks) > 16:
            raise ValueError('invalid checks')
        for check in checks:
            if (
                not isinstance(check, dict)
                or set(check) != {'kind', 'value'}
                or check.get('kind') not in {'literal', 'regex'}
                or not isinstance(check.get('value'), str)
                or not check['value']
            ):
                raise ValueError('invalid check')
            if check['kind'] == 'regex':
                safe_regex(check['value'])
            elif len(check['value'].encode('utf-8')) > MAX_MATCH_TEXT_LENGTH:
                raise ValueError('literal check too large')
        if (
            'review_status' in case
            and case['review_status'] not in {'pending', 'reviewed'}
        ):
            raise ValueError('invalid review status')
        if 'reviewer' in case and case['reviewer'] is not None and not _text(
            case['reviewer']
        ):
            raise ValueError('invalid reviewer')
        definition = _definition(case)
        duplicate = any(
            item['question'] == definition['question']
            and item['query_scope'] == definition['query_scope']
            for item in key_records
        )
        if duplicate:
            raise ValueError('duplicate question and scope')
        key_records.append(definition)
        counts[split] += 1
        scenarios[split].add(scenario)
    required = {'answerable', 'no_answer', 'conflict', 'stale'}
    if require_complete and counts['dev'] and not required <= scenarios['dev']:
        raise ValueError('dev split is missing required scenarios')
    return value


def load_dataset(path: str | Path) -> dict:
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError:
        raise ValueError('dataset is unreadable') from None
    if len(raw) > MAX_DATASET_BYTES:
        raise ValueError('dataset is too large')
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ValueError('dataset is not valid JSON') from None
    return validate_dataset(value, require_complete=True)


def dataset_fingerprint(dataset: dict) -> str:
    validate_dataset(dataset, require_complete=True)
    definitions = sorted(
        (_definition(case) for case in dataset['cases']),
        key=lambda item: item['id'],
    )
    encoded = canonical(
        {
            'schema_version': dataset['schema_version'],
            'dataset_kind': dataset['dataset_kind'],
            'cases': definitions,
        }
    )
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest()


def select_cases(dataset: dict, split: str) -> list[dict]:
    validate_dataset(dataset)
    if split not in SPLITS:
        raise ValueError('unsupported split')
    selected = [
        case for case in dataset['cases'] if case.get('split', 'dev') == split
    ]
    if split == 'holdout' and selected and {
        case['scenario'] for case in selected
    } != SCENARIOS:
        raise ValueError('holdout split must cover all scenarios')
    return selected


def validate_manifest(actual: dict, expected: dict) -> None:
    required = {
        'experiment_id',
        'dataset_hash',
        'config_hash',
        'seed_hash',
        'git_commit',
        'graph_id',
        'profile_ref',
        'model_refs',
        'kb_versions',
        'repeats',
        'split',
        'started_at',
        'runner_version',
        'scoring_version',
        'limits',
        'comparability',
    }
    for name, value in (('actual', actual), ('expected', expected)):
        if not isinstance(value, dict) or not required <= set(value):
            raise ValueError(f'{name} manifest is incomplete')
    immutable = {
        'experiment_id',
        'dataset_hash',
        'config_hash',
        'seed_hash',
        'repeats',
        'split',
        'runner_version',
        'scoring_version',
        'limits',
    }
    changed = sorted(
        key for key in immutable if canonical(actual[key]) != canonical(expected[key])
    )
    if changed:
        raise ValueError('experiment identity changed: ' + ', '.join(changed))
    for key in ('graph_id', 'profile_ref', 'model_refs', 'kb_versions'):
        if canonical(actual[key]) != canonical(expected[key]):
            raise ValueError('experiment versions changed: ' + key)
    if expected.get('comparability') == 'available':
        return
    if actual.get('comparability') != expected.get('comparability'):
        raise ValueError('experiment comparability changed')

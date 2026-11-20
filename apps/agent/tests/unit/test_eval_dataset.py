import json

import pytest

from workbench_agent.evaluation.dataset import (
    dataset_fingerprint,
    load_dataset,
    safe_regex,
    select_cases,
    validate_dataset,
    validate_manifest,
)


def scope(index=0):
    return {
        'service': 'checkout',
        'environment': 'test',
        'region': 'id',
        'start_at': f'2026-11-20T00:{index:02d}:00Z',
        'end_at': f'2026-11-20T00:{index + 1:02d}:00Z',
    }


def case(case_id, scenario='answerable', *, split='dev', index=0):
    answerable = scenario in {'answerable', 'conflict', 'stale', 'multiple_sources'}
    return {
        'id': case_id,
        'question': f'Question {case_id}?',
        'query_scope': scope(index),
        'scenario': scenario,
        'answerable': answerable,
        'expected_source_ids': ['kb:entry@1#chunk'] if answerable else [],
        'must_include': ['fact'] if answerable else [],
        'review_status': 'reviewed',
        'reviewer': 'owner',
        'split': split,
        'checks': [],
    }


def dataset():
    return {
        'schema_version': 'knowledge-eval-v1',
        'dataset_kind': 'business',
        'cases': [
            case('a', index=0),
            case('b', 'no_answer', index=1),
            case('c', 'conflict', index=2),
            case('d', 'stale', index=3),
        ],
    }


def test_dataset_hash_is_stable_and_excludes_result_review():
    first = dataset()
    reordered = json.loads(json.dumps(first))
    reordered['cases'].reverse()
    assert dataset_fingerprint(first) == dataset_fingerprint(reordered)
    first['cases'][0]['result_review'] = {'answer_usable': True}
    assert dataset_fingerprint(first) == dataset_fingerprint(reordered)
    first['cases'][0]['must_include'].append('new fact')
    assert dataset_fingerprint(first) != dataset_fingerprint(reordered)


def test_duplicate_normalized_scope_and_bad_labels_are_rejected():
    data = dataset()
    duplicate = case('e', index=0)
    duplicate['question'] = '  QUESTION A?  '
    data['cases'].append(duplicate)
    with pytest.raises(ValueError, match='duplicate'):
        validate_dataset(data, require_complete=True)
    data = dataset()
    data['cases'][0]['scenario'] = 'made-up'
    with pytest.raises(ValueError, match='scenario'):
        validate_dataset(data, require_complete=True)
    data = dataset()
    data['cases'][0]['expected_source_ids'] = []
    with pytest.raises(ValueError, match='sources'):
        validate_dataset(data, require_complete=True)


def test_empty_template_is_valid_but_holdout_requires_coverage():
    empty = {
        'schema_version': 'knowledge-eval-v1',
        'dataset_kind': 'business',
        'cases': [],
    }
    assert validate_dataset(empty) is empty
    partial = {
        **empty,
        'cases': [case('h', 'answerable', split='holdout')],
    }
    assert validate_dataset(partial) is partial


@pytest.mark.parametrize(
    'pattern',
    [r'(a+)+$', r'(?=secret)', r'(a)\1', 'x' * 257, '('],
)
def test_unsafe_or_invalid_regex_is_rejected(pattern):
    with pytest.raises(ValueError):
        safe_regex(pattern)


def test_literal_and_simple_regex_check():
    data = dataset()
    data['cases'][0]['checks'] = [
        {'kind': 'literal', 'value': 'fact'},
        {'kind': 'regex', 'value': r'^fact$'},
    ]
    assert validate_dataset(data, require_complete=True) is data
    assert safe_regex(r'^fact$').search('fact')


def test_split_selection_and_file_load(tmp_path):
    data = dataset()
    data['cases'].extend([
        case('h1', 'answerable', split='holdout', index=4),
        case('h2', 'multiple_sources', split='holdout', index=5),
        case('h3', 'no_answer', split='holdout', index=6),
        case('h4', 'conflict', split='holdout', index=7),
        case('h5', 'stale', split='holdout', index=8),
        case('h6', 'unauthorized', split='holdout', index=9),
    ])
    path = tmp_path / 'dataset.json'
    path.write_text(json.dumps(data))
    loaded = load_dataset(path)
    assert [item['id'] for item in select_cases(loaded, 'dev')] == ['a', 'b', 'c', 'd']
    assert [item['id'] for item in select_cases(loaded, 'holdout')] == [
        'h1', 'h2', 'h3', 'h4', 'h5', 'h6'
    ]


def test_manifest_rejects_identity_and_version_changes():
    expected = {
        'experiment_id': 'e1',
        'dataset_hash': 'd',
        'config_hash': 'c',
        'seed_hash': 's',
        'git_commit': 'a' * 40,
        'graph_id': 'knowledge.citation_review',
        'profile_ref': {'resource_id': 'p', 'version': '1', 'content_hash': 'a' * 64},
        'model_refs': [],
        'kb_versions': ['kb@1'],
        'repeats': 3,
        'split': 'dev',
        'started_at': '2026-11-20T00:00:00Z',
        'runner_version': 'v1',
        'scoring_version': 'v1',
        'limits': {'tokens': 1},
        'comparability': 'available',
    }
    actual = {**expected}
    validate_manifest(actual, expected)
    actual['repeats'] = 2
    with pytest.raises(ValueError, match='repeats'):
        validate_manifest(actual, expected)
    actual = {**expected, 'graph_id': 'other'}
    with pytest.raises(ValueError, match='graph_id'):
        validate_manifest(actual, expected)

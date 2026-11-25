import json
from pathlib import Path

import pytest

from workbench_agent.evaluation.dataset import validate_dataset
from workbench_agent.evaluation.metrics import apply_dataset_boundary


FIXTURE = Path(__file__).parents[1] / 'fixtures/knowledge-eval-synthetic.json'


def _synthetic():
    value = json.loads(FIXTURE.read_text(encoding='utf-8'))
    value['schema_version'] = 'knowledge-eval-v1'
    value.pop('description', None)
    for index, case in enumerate(value['cases']):
        case['question'] = f'{case["question"]} #{index + 1}'
    return value


def test_synthetic_dataset_requires_engineering_purpose():
    with pytest.raises(ValueError, match='business'):
        validate_dataset(_synthetic(), require_complete=True)
    assert (
        validate_dataset(_synthetic(), require_complete=True, purpose='engineering')['dataset_kind']
        == 'synthetic'
    )


def test_synthetic_holdout_is_not_accepted_for_engineering_regression():
    dataset = _synthetic()
    dataset['cases'][0]['split'] = 'holdout'
    with pytest.raises(ValueError, match='dev'):
        validate_dataset(dataset, require_complete=True, purpose='engineering')


def test_synthetic_report_cannot_pass_release():
    assert apply_dataset_boundary(
        {
            'metrics': {'rule_rate': 1.0},
            'quality_status': 'passed',
            'release_passed': True,
        },
        dataset_kind='synthetic',
    ) == {
        'metrics': {'rule_rate': 1.0},
        'quality_status': 'unavailable',
        'release_passed': False,
        'dataset_kind': 'synthetic',
    }


def test_business_dataset_keeps_existing_quality_semantics():
    dataset = _synthetic()
    dataset['dataset_kind'] = 'business'
    assert validate_dataset(dataset, require_complete=True, purpose='business') is dataset
    report = {'quality_status': 'passed', 'release_passed': True}
    assert apply_dataset_boundary(report, dataset_kind='business') == {
        **report,
        'dataset_kind': 'business',
    }


def test_synthetic_and_business_reports_are_not_comparable():
    from workbench_agent.evaluation.comparison import compare_reports

    result = compare_reports(
        {'dataset_kind': 'business', 'metrics': {}},
        {'dataset_kind': 'synthetic', 'metrics': {}},
        {},
    )
    assert result['comparable'] is False
    assert result['status'] != 'passed'

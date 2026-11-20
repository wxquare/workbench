import json
import subprocess
import sys
from pathlib import Path


def case(case_id, scenario, index, *, answerable):
    return {
        'id': case_id,
        'question': f'Question {case_id}?',
        'query_scope': {
            'service': 'checkout',
            'environment': 'test',
            'region': 'id',
            'start_at': f'2026-11-20T00:{index:02d}:00Z',
            'end_at': f'2026-11-20T00:{index + 1:02d}:00Z',
        },
        'scenario': scenario,
        'answerable': answerable,
        'expected_source_ids': ['kb:entry@1#chunk'] if answerable else [],
        'must_include': ['fact'] if answerable else [],
        'review_status': 'reviewed',
        'reviewer': 'owner',
        'split': 'dev',
        'checks': [],
    }


def dataset():
    return {
        'schema_version': 'knowledge-eval-v1',
        'dataset_kind': 'business',
        'cases': [
            case('a', 'answerable', 0, answerable=True),
            case('b', 'no_answer', 1, answerable=False),
            case('c', 'conflict', 2, answerable=True),
            case('d', 'stale', 3, answerable=True),
        ],
    }


def run_cli(*args, cwd=None):
    return subprocess.run(
        [sys.executable, '-m', 'workbench_agent.cli.main', *map(str, args)],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def test_eval_validate_accepts_versioned_dataset(tmp_path):
    path = tmp_path / 'dataset.json'
    path.write_text(json.dumps(dataset()))
    result = run_cli('eval', 'validate', '--dataset', path)
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output['valid'] is True
    assert output['cases'] == 4
    assert len(output['dataset_hash']) == 64


def test_eval_validate_rejects_invalid_data_and_does_not_need_config(tmp_path):
    path = tmp_path / 'dataset.json'
    data = dataset()
    data['cases'][0]['checks'] = [{'kind': 'regex', 'value': '(a+)+$'}]
    path.write_text(json.dumps(data))
    result = run_cli('eval', 'validate', '--dataset', path)
    assert result.returncode == 2
    assert 'invalid' in result.stdout


def test_legacy_evaluate_still_works(tmp_path):
    output = tmp_path / 'report.json'
    fixture = Path(__file__).parents[1] / 'fixtures/business/manifest.json'
    result = run_cli('evaluate', '--dataset', fixture, '--output', output)
    assert result.returncode == 1
    assert json.loads(output.read_text())['release_passed'] is False

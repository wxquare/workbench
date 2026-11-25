import json
import subprocess
import sys
from pathlib import Path

import pytest


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


def holdout_dataset():
    scenarios = ('answerable', 'multiple_sources', 'no_answer', 'conflict', 'stale', 'unauthorized')
    return {
        'schema_version': 'knowledge-eval-v1',
        'dataset_kind': 'business',
        'cases': [
            {
                **case(f'h{index}', scenario, index, answerable=scenario != 'no_answer'),
                'split': 'holdout',
            }
            for index, scenario in enumerate(scenarios)
        ],
    }


def write_score_manifest(experiment, data, split):
    from workbench_agent.evaluation.dataset import dataset_fingerprint
    from workbench_agent.evaluation.tool_conversation_eval import tool_dataset_fingerprint

    fingerprint = (
        tool_dataset_fingerprint(data)
        if data['schema_version'] == 'tool-conversation-eval-v1'
        else dataset_fingerprint(data)
    )
    (experiment / 'manifest.json').write_text(
        json.dumps({'dataset_hash': fingerprint, 'split': split})
    )


def test_scoring_rejects_relabelled_holdout_cases_without_permit(tmp_path):
    original = holdout_dataset()
    altered = {**original, 'cases': [{**item, 'split': 'dev'} for item in original['cases']]}
    dataset_path = tmp_path / 'dataset.json'
    dataset_path.write_text(json.dumps(altered))
    experiment = tmp_path / 'experiment'
    experiment.mkdir()
    write_score_manifest(experiment, original, 'holdout')
    (experiment / 'results.jsonl').write_text('')
    output = tmp_path / 'report.json'
    result = run_cli(
        'eval', 'score', '--experiment', experiment, '--dataset', dataset_path, '--output', output
    )
    assert result.returncode == 2
    assert not output.exists()


def test_scoring_requires_an_experiment_manifest(tmp_path):
    dataset_path = tmp_path / 'dataset.json'
    dataset_path.write_text(json.dumps(dataset()))
    experiment = tmp_path / 'experiment'
    experiment.mkdir()
    (experiment / 'results.jsonl').write_text('')
    output = tmp_path / 'report.json'
    result = run_cli(
        'eval', 'score', '--experiment', experiment, '--dataset', dataset_path, '--output', output
    )
    assert result.returncode == 2
    assert not output.exists()


def test_score_reads_legacy_experiment_manifest(tmp_path):
    dataset_path = tmp_path / 'dataset.json'
    dataset_path.write_text(json.dumps(dataset()))
    experiment = tmp_path / 'experiment'
    experiment.mkdir()
    write_score_manifest(experiment, dataset(), 'dev')
    (experiment / 'manifest.json').rename(experiment / 'experiment.json')
    (experiment / 'results.jsonl').write_text('')
    output = tmp_path / 'report.json'
    result = run_cli(
        'eval', 'score', '--experiment', experiment, '--dataset', dataset_path, '--output', output
    )
    assert result.returncode == 1
    assert output.exists()


def test_legacy_holdout_score_still_requires_permit(tmp_path):
    dataset_path = tmp_path / 'dataset.json'
    dataset_path.write_text(json.dumps(holdout_dataset()))
    experiment = tmp_path / 'experiment'
    experiment.mkdir()
    write_score_manifest(experiment, holdout_dataset(), 'holdout')
    (experiment / 'manifest.json').rename(experiment / 'experiment.json')
    (experiment / 'results.jsonl').write_text('')
    output = tmp_path / 'report.json'
    args = (
        'eval',
        'score',
        '--experiment',
        experiment,
        '--dataset',
        dataset_path,
        '--split',
        'holdout',
        '--output',
        output,
    )
    assert run_cli(*args).returncode == 2
    assert not output.exists()
    permit = tmp_path / 'permit.json'
    permit.write_text(json.dumps({'release_id': 'release-1', 'active': True}))
    ledger = tmp_path / 'access.jsonl'
    result = run_cli(
        *args,
        '--purpose',
        'release',
        '--release-id',
        'release-1',
        '--holdout-permit',
        permit,
        '--holdout-ledger',
        ledger,
    )
    assert result.returncode == 1
    assert output.exists()
    assert json.loads(ledger.read_text())['release_id'] == 'release-1'


def test_new_run_rejects_existing_experiment_directory(tmp_path, monkeypatch):
    from argparse import Namespace
    import pytest
    from workbench_agent.cli import eval as eval_cli

    dataset_path = tmp_path / 'dataset.json'
    dataset_path.write_text(json.dumps(dataset()))
    seed = tmp_path / 'seed.json'
    seed.write_text('{}')
    config_path = tmp_path / 'config.json'
    config_path.write_text(
        json.dumps(
            {
                'seed_manifest_path': str(seed),
                'expected_graph_id': 'knowledge.citation_review',
                'limits': {'model_calls': 1, 'tool_calls': 1, 'tokens': 100, 'wall_seconds': 60},
                'output_root': str(tmp_path / 'output'),
                'token_env': 'HOLDOUT_TEST_TOKEN',
                'api_base_url': 'http://127.0.0.1:8000',
                'request_timeout_seconds': 1,
                'case_timeout_seconds': 1,
            }
        )
    )
    experiment = tmp_path / 'output' / 'exp'
    experiment.mkdir(parents=True)
    original = json.dumps({'split': 'holdout', 'dataset_hash': 'a' * 64})
    (experiment / 'manifest.json').write_text(original)
    monkeypatch.setenv('HOLDOUT_TEST_TOKEN', 'fixture-token')
    calls = []

    async def fake_runner(*args, **kwargs):
        calls.append(True)
        return {'rows': [], 'complete': True, 'expected': 0}

    monkeypatch.setattr(eval_cli, 'run_experiment', fake_runner)
    args = Namespace(
        eval_command='run',
        purpose=None,
        resume=None,
        dataset=str(dataset_path),
        config=str(config_path),
        experiment_id='exp',
        split='dev',
        repeats=1,
    )
    with pytest.raises(ValueError):
        eval_cli.main(args)
    assert calls == []
    assert (experiment / 'manifest.json').read_text() == original


def test_holdout_score_requires_a_release_permit_before_writing_report(tmp_path):
    dataset_path = tmp_path / 'dataset.json'
    dataset_path.write_text(json.dumps(holdout_dataset()))
    experiment = tmp_path / 'experiment'
    experiment.mkdir()
    write_score_manifest(experiment, holdout_dataset(), 'holdout')
    (experiment / 'results.jsonl').write_text('')
    output = tmp_path / 'report.json'
    result = run_cli(
        'eval',
        'score',
        '--experiment',
        experiment,
        '--dataset',
        dataset_path,
        '--split',
        'holdout',
        '--output',
        output,
    )
    assert result.returncode == 2
    assert not output.exists()


def test_holdout_run_requires_permit_before_creating_experiment(tmp_path):
    dataset_path = tmp_path / 'dataset.json'
    dataset_path.write_text(json.dumps(holdout_dataset()))
    seed = tmp_path / 'seed.json'
    seed.write_text('{}')
    config_path = tmp_path / 'config.json'
    config_path.write_text(
        json.dumps(
            {
                'seed_manifest_path': str(seed),
                'expected_graph_id': 'knowledge.citation_review',
                'limits': {'model_calls': 1, 'tool_calls': 1, 'tokens': 100, 'wall_seconds': 60},
                'output_root': str(tmp_path / 'output'),
                'token_env': 'NOT_SET_FOR_HOLDOUT_TEST',
            }
        )
    )
    result = run_cli(
        'eval',
        'run',
        '--dataset',
        dataset_path,
        '--config',
        config_path,
        '--experiment-id',
        'exp',
        '--split',
        'holdout',
        '--purpose',
        'release',
    )
    assert result.returncode == 2
    assert not (tmp_path / 'output' / 'exp').exists()


def test_holdout_score_records_permitted_release_once(tmp_path):
    dataset_path = tmp_path / 'dataset.json'
    dataset_path.write_text(json.dumps(holdout_dataset()))
    experiment = tmp_path / 'experiment'
    experiment.mkdir()
    write_score_manifest(experiment, holdout_dataset(), 'holdout')
    (experiment / 'results.jsonl').write_text('')
    permit = tmp_path / 'permit.json'
    permit.write_text(json.dumps({'release_id': 'release-1', 'active': True}))
    ledger = tmp_path / 'access.jsonl'
    output = tmp_path / 'report.json'
    args = (
        'eval',
        'score',
        '--experiment',
        experiment,
        '--dataset',
        dataset_path,
        '--split',
        'holdout',
        '--output',
        output,
        '--purpose',
        'release',
        '--release-id',
        'release-1',
        '--holdout-permit',
        permit,
        '--holdout-ledger',
        ledger,
    )
    assert run_cli(*args).returncode == 1
    assert run_cli(*args).returncode == 1
    assert output.exists()
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]['release_id'] == 'release-1'
    assert len(rows[0]['dataset_hash']) == 64
    assert 'question' not in rows[0]


def test_holdout_run_with_permit_records_access_before_execution(tmp_path, monkeypatch):
    from argparse import Namespace
    from workbench_agent.cli import eval as eval_cli

    dataset_path = tmp_path / 'dataset.json'
    dataset_path.write_text(json.dumps(holdout_dataset()))
    seed = tmp_path / 'seed.json'
    seed.write_text('{}')
    config_path = tmp_path / 'config.json'
    config_path.write_text(
        json.dumps(
            {
                'seed_manifest_path': str(seed),
                'expected_graph_id': 'knowledge.citation_review',
                'limits': {'model_calls': 1, 'tool_calls': 1, 'tokens': 100, 'wall_seconds': 60},
                'output_root': str(tmp_path / 'output'),
                'token_env': 'HOLDOUT_TEST_TOKEN',
                'api_base_url': 'http://127.0.0.1:8000',
                'request_timeout_seconds': 1,
                'case_timeout_seconds': 1,
            }
        )
    )
    permit = tmp_path / 'permit.json'
    permit.write_text(json.dumps({'release_id': 'release-1', 'active': True}))
    ledger = tmp_path / 'access.jsonl'
    monkeypatch.setenv('HOLDOUT_TEST_TOKEN', 'fixture-token')

    async def fake_runner(dataset, config, **kwargs):
        assert kwargs['split'] == 'holdout'
        assert json.loads(ledger.read_text())['release_id'] == 'release-1'
        return {'rows': [], 'complete': True, 'expected': 0}

    monkeypatch.setattr(eval_cli, 'run_experiment', fake_runner)
    args = Namespace(
        eval_command='run',
        purpose='release',
        resume=None,
        dataset=str(dataset_path),
        config=str(config_path),
        experiment_id='exp',
        split='holdout',
        repeats=1,
        holdout_permit=str(permit),
        holdout_ledger=str(ledger),
        release_id='release-1',
    )
    assert eval_cli.main(args) == 0
    assert (tmp_path / 'output' / 'exp' / 'run-summary.json').exists()


def test_synthetic_experiment_resume_uses_frozen_engineering_purpose(tmp_path, monkeypatch):
    from argparse import Namespace
    from workbench_agent.cli import eval as eval_cli

    data = {**dataset(), 'dataset_kind': 'synthetic'}
    dataset_path = tmp_path / 'dataset.json'
    dataset_path.write_text(json.dumps(data))
    seed = tmp_path / 'seed.json'
    seed.write_text('{}')
    config_path = tmp_path / 'config.json'
    config_path.write_text(
        json.dumps(
            {
                'seed_manifest_path': str(seed),
                'expected_graph_id': 'knowledge.citation_review',
                'limits': {'model_calls': 1, 'tool_calls': 1, 'tokens': 100, 'wall_seconds': 60},
                'output_root': str(tmp_path / 'output'),
                'token_env': 'ENGINEERING_TEST_TOKEN',
                'api_base_url': 'http://127.0.0.1:8000',
                'request_timeout_seconds': 1,
                'case_timeout_seconds': 1,
            }
        )
    )
    monkeypatch.setenv('ENGINEERING_TEST_TOKEN', 'fixture-token')
    purposes = []

    async def fake_runner(value, config, **kwargs):
        purposes.append(kwargs['manifest']['purpose'])
        return {'rows': [], 'complete': True, 'expected': 0}

    monkeypatch.setattr(eval_cli, 'run_experiment', fake_runner)
    args = Namespace(
        eval_command='run',
        purpose='engineering',
        resume=None,
        dataset=str(dataset_path),
        config=str(config_path),
        experiment_id='exp',
        split='dev',
        repeats=1,
    )
    assert eval_cli.main(args) == 0
    args.resume = str(tmp_path / 'output/exp')
    args.dataset = None
    args.experiment_id = None
    args.split = None
    args.purpose = None
    assert eval_cli.main(args) == 0
    assert purposes == ['engineering', 'engineering']
    args.purpose = 'business'
    with pytest.raises(ValueError, match='resume purpose does not match manifest'):
        eval_cli.main(args)


def test_resume_holdout_with_default_split_rechecks_current_permit(tmp_path):
    from workbench_agent.evaluation.dataset import dataset_fingerprint

    dataset_path = tmp_path / 'dataset.json'
    dataset_path.write_text(json.dumps(holdout_dataset()))
    config_path = tmp_path / 'config.json'
    config_path.write_text(json.dumps({'token_env': 'NOT_SET_FOR_HOLDOUT_TEST'}))
    experiment = tmp_path / 'experiment'
    experiment.mkdir()
    (experiment / 'experiment.json').write_text(
        json.dumps(
            {
                'split': 'holdout',
                'repeats': 1,
                'dataset_path': str(dataset_path),
                'config_path': str(config_path),
                'experiment_id': 'exp',
                'dataset_hash': dataset_fingerprint(holdout_dataset()),
            }
        )
    )
    result = run_cli('eval', 'run', '--resume', experiment)
    assert result.returncode == 2
    assert not (experiment / 'manifest.json').exists()


def test_resume_existing_manifest_authorizes_its_holdout_split(tmp_path):
    from workbench_agent.evaluation.dataset import dataset_fingerprint

    dataset_path = tmp_path / 'dataset.json'
    dataset_path.write_text(json.dumps(holdout_dataset()))
    config_path = tmp_path / 'config.json'
    config_path.write_text(json.dumps({'token_env': 'NOT_SET_FOR_HOLDOUT_TEST'}))
    experiment = tmp_path / 'experiment'
    experiment.mkdir()
    manifest = experiment / 'manifest.json'
    fingerprint = dataset_fingerprint(holdout_dataset())
    manifest.write_text(
        json.dumps(
            {
                'split': 'holdout',
                'repeats': 1,
                'dataset_path': str(dataset_path),
                'config_path': str(config_path),
                'experiment_id': 'exp',
                'dataset_hash': fingerprint,
            }
        )
    )
    permit = tmp_path / 'permit.json'
    permit.write_text(json.dumps({'release_id': 'release-1', 'active': True}))
    ledger = tmp_path / 'access.jsonl'
    result = run_cli(
        'eval',
        'run',
        '--resume',
        experiment,
        '--purpose',
        'release',
        '--release-id',
        'release-1',
        '--holdout-permit',
        permit,
        '--holdout-ledger',
        ledger,
    )
    assert result.returncode == 2  # The test deliberately provides no API token.
    assert json.loads(ledger.read_text())['dataset_hash'] == fingerprint


def test_resume_rejects_dataset_changed_since_experiment_before_ledger(tmp_path):
    dataset_path = tmp_path / 'dataset.json'
    dataset_path.write_text(json.dumps(holdout_dataset()))
    config_path = tmp_path / 'config.json'
    config_path.write_text(json.dumps({'token_env': 'NOT_SET_FOR_HOLDOUT_TEST'}))
    experiment = tmp_path / 'experiment'
    experiment.mkdir()
    (experiment / 'manifest.json').write_text(
        json.dumps(
            {
                'split': 'holdout',
                'repeats': 1,
                'dataset_path': str(dataset_path),
                'config_path': str(config_path),
                'experiment_id': 'exp',
                'dataset_hash': 'a' * 64,
            }
        )
    )
    permit = tmp_path / 'permit.json'
    permit.write_text(json.dumps({'release_id': 'release-1', 'active': True}))
    ledger = tmp_path / 'access.jsonl'
    result = run_cli(
        'eval',
        'run',
        '--resume',
        experiment,
        '--purpose',
        'release',
        '--release-id',
        'release-1',
        '--holdout-permit',
        permit,
        '--holdout-ledger',
        ledger,
    )
    assert result.returncode == 2
    assert not ledger.exists()


def test_holdout_invalid_permit_and_corrupt_ledger_fail_closed(tmp_path):
    dataset_path = tmp_path / 'dataset.json'
    dataset_path.write_text(json.dumps(holdout_dataset()))
    experiment = tmp_path / 'experiment'
    experiment.mkdir()
    write_score_manifest(experiment, holdout_dataset(), 'holdout')
    (experiment / 'results.jsonl').write_text('')
    permit = tmp_path / 'permit.json'
    ledger = tmp_path / 'access.jsonl'
    output = tmp_path / 'report.json'
    args = (
        'eval',
        'score',
        '--experiment',
        experiment,
        '--dataset',
        dataset_path,
        '--split',
        'holdout',
        '--output',
        output,
        '--purpose',
        'release',
        '--release-id',
        'release-1',
        '--holdout-permit',
        permit,
        '--holdout-ledger',
        ledger,
    )
    permit.write_text(json.dumps({'release_id': 'other', 'active': True}))
    assert run_cli(*args).returncode == 2
    assert not ledger.exists() and not output.exists()
    permit.write_text(json.dumps({'release_id': 'release-1', 'active': True}))
    ledger.write_text('invalid json\n')
    assert run_cli(*args).returncode == 2
    assert not output.exists()


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


def test_score_and_compare_offline_reports(tmp_path):
    dataset_path = tmp_path / 'dataset.json'
    dataset_path.write_text(json.dumps(dataset()))
    experiment = tmp_path / 'experiment'
    experiment.mkdir()
    write_score_manifest(experiment, dataset(), 'dev')
    rows = []
    for item in dataset()['cases']:
        rows.append(
            {
                'case_id': item['id'],
                'repeat': 0,
                'transport_status': 'completed',
                'run_status': 'succeeded',
                'outcome': {
                    'summary': 'fact retry' if item['answerable'] else 'Insufficient evidence.',
                    'knowledge_answer': {
                        'claims': [
                            {
                                'text': 'fact retry',
                                'citations': [
                                    {
                                        'evidence_ref': 'ev',
                                        'source_id': 'kb:entry@1#chunk',
                                        'quote': 'fact retry',
                                    }
                                ],
                            }
                        ]
                    }
                    if item['answerable']
                    else [],
                },
                'evidence': [{'evidence_id': 'ev', 'content': {'content': 'fact retry'}}],
                'usage': {'amount_micros': 0, 'currency': 'USD', 'cost_pending': False},
                'reviewed_unanswerable': not item['answerable'],
            }
        )
    (experiment / 'results.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in rows))
    report = tmp_path / 'report.json'
    result = run_cli(
        'eval',
        'score',
        '--experiment',
        experiment,
        '--dataset',
        dataset_path,
        '--output',
        report,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    input_data = json.loads(report.read_text())
    assert input_data['metrics']['complete'] is True
    second = tmp_path / 'second'
    second.mkdir()
    (second / 'report.json').write_text(report.read_text())
    comparison = tmp_path / 'comparison.json'
    policy = Path(__file__).parents[4] / 'evaluation/knowledge/regression-policy.json'
    result = run_cli(
        'eval',
        'compare',
        '--baseline',
        second,
        '--candidate',
        second,
        '--policy',
        policy,
        '--output',
        comparison,
    )
    assert result.returncode == 0
    assert json.loads(comparison.read_text())['status'] == 'passed'


def test_tool_conversation_scoring_uses_its_own_dataset_contract(tmp_path):
    template = Path(__file__).parents[4] / 'evaluation/tool-conversation/dataset.example.json'
    dataset = json.loads(template.read_text())
    dataset['cases'] = dataset['cases'][:1]
    dataset['cases'][0]['id'] = 'tool-rule'
    dataset['cases'][0]['scenario'] = 'retrieval_required'
    dataset_path = tmp_path / 'tool-dataset.json'
    dataset_path.write_text(json.dumps(dataset))
    experiment = tmp_path / 'tool-experiment'
    experiment.mkdir()
    write_score_manifest(experiment, dataset, 'dev')
    summary = (
        '以下回答基于所列工具返回的结果，未经独立校验\n\n'
        'answer\n\n工具依据：\n- knowledge.search / call-1 / ok'
    )
    (experiment / 'results.jsonl').write_text(
        json.dumps(
            {
                'case_id': 'tool-rule',
                'repeat': 0,
                'transport_status': 'completed',
                'run_status': 'succeeded',
                'outcome': {
                    'completed': True,
                    'summary': summary,
                    'evidence_refs': ['ev-1'],
                    'unresolved_items': [],
                },
                'evidence': [{'evidence_id': 'ev-1', 'content': {'status': 'ok'}}],
            }
        )
        + '\n'
    )
    report = tmp_path / 'tool-report.json'

    result = run_cli(
        'eval',
        'score',
        '--experiment',
        experiment,
        '--dataset',
        dataset_path,
        '--output',
        report,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    value = json.loads(report.read_text())
    assert value['scoring_version'] == 'tool-conversation-rules-v1'
    assert value['metrics']['rule_rate'] == 1


def test_tool_dataset_validation_uses_tool_fingerprint(tmp_path):
    template = Path(__file__).parents[4] / 'evaluation/tool-conversation/dataset.example.json'
    dataset = json.loads(template.read_text())
    dataset['cases'] = dataset['cases'][:1]
    path = tmp_path / 'tool-dataset.json'
    path.write_text(json.dumps(dataset))

    result = run_cli('eval', 'validate', '--dataset', path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert len(json.loads(result.stdout)['dataset_hash']) == 64


def test_tool_dataset_validation_is_strict(tmp_path):
    template = Path(__file__).parents[4] / 'evaluation/tool-conversation/dataset.example.json'
    dataset = json.loads(template.read_text())
    dataset['cases'] = dataset['cases'][:2]
    dataset['cases'][0]['scenario'] = 'unknown'
    path = tmp_path / 'invalid-tool-dataset.json'
    path.write_text(json.dumps(dataset))

    result = run_cli('eval', 'validate', '--dataset', path)

    assert result.returncode == 2

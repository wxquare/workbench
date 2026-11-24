from workbench_agent.evaluation.comparison import compare_reports


def report(**changes):
    value = {
        'dataset_hash': 'd',
        'dataset_kind': 'business',
        'purpose': 'business',
        'split': 'dev',
        'repeats': 3,
        'scoring_version': 'v1',
        'metrics': {
            'rule_rate': .9,
            'latency_p95': 100,
            'unauthorized_accesses': 0,
            'incorrect_conclusions': 0,
        },
    }
    value['metrics'].update(changes)
    return value


def test_incompatible_metadata_is_not_called_passed():
    candidate = report()
    candidate['dataset_hash'] = 'other'
    assert compare_reports(report(), candidate, {})['status'] == 'incomparable'


def test_new_unsafe_conclusion_is_a_regression():
    result = compare_reports(report(), report(incorrect_conclusions=1), {})
    assert result['status'] == 'regressed'
    assert 'incorrect_conclusions_regression' in result['regressions']


def test_rate_and_latency_thresholds_are_explicit():
    result = compare_reports(
        report(),
        report(rule_rate=.8, latency_p95=130),
        {'max_rule_rate_drop': .05},
    )
    assert 'rule_rate_regression' in result['regressions']
    assert 'latency_regression' in result['regressions']

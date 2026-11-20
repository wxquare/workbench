from workbench_agent.evaluation.metrics import summarize_repeats


def test_repeats_do_not_hide_flaky_or_missing_case():
    scores = [
        {'case_id': 'a', 'repeat': 0, 'rules_status': 'passed'},
        {'case_id': 'a', 'repeat': 1, 'rules_status': 'failed'},
        {'case_id': 'a', 'repeat': 2, 'rules_status': 'passed'},
        {'case_id': 'b', 'repeat': 0, 'rules_status': 'passed'},
    ]
    report = summarize_repeats(scores, expected_cases=['a', 'b'], k=3)
    assert report['empirical_pass_at_k'] == 1.0
    assert report['empirical_pass_power_k'] == 0.0
    assert report['missing_runs'] == 2
    assert report['complete'] is False


def test_cost_unknown_and_multicurrency_are_not_coerced():
    scores = [
        {'case_id': 'a', 'repeat': 0, 'rules_status': 'passed',
         'usage': {'amount_micros': 0, 'currency': 'USD', 'cost_pending': False}},
        {'case_id': 'a', 'repeat': 1, 'rules_status': 'passed',
         'usage': {'amount_micros': 1, 'currency': 'CNY', 'cost_pending': False}},
        {'case_id': 'a', 'repeat': 2, 'rules_status': 'passed',
         'usage': {'amount_micros': None, 'currency': None, 'cost_pending': True}},
    ]
    report = summarize_repeats(scores, expected_cases=['a'], k=3)
    assert report['known_costs'] == {'USD': 0, 'CNY': 1}
    assert report['pending_cost_runs'] == 1

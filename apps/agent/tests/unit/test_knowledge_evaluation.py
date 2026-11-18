import json

import pytest

from workbench_agent.evaluation.knowledge_report import evaluate_knowledge_dataset


def case(index, answerable=True):
    return {'id': str(index), 'question': f'What is the limit for example {index}?',
            'query_scope': {'service': 'example', 'environment': 'test', 'region': 'sg',
                            'start_at': '2026-11-18T00:00:00Z', 'end_at': '2026-11-18T01:00:00Z'},
            'expected_source_ids': ['doc-1'], 'must_include': ['limit is 3'],
            'answerable': answerable, 'scenario': 'answerable' if answerable else 'no_answer',
            'review_status': 'reviewed', 'reviewer': 'business reviewer',
            'result_review': {'reviewer': 'business reviewer', 'output_present': True,
                              'output_ref': f'run-{index}', 'claims': [{'citation_correct': True}] if answerable else [],
                              'answer_usable': answerable, 'unauthorized_access': False,
                              'degradation_reason': None if answerable else 'no_answer'}}


def dataset():
    data = {'dataset_kind': 'business', 'cases': [case(i, i < 27) for i in range(30)]}
    data['cases'][26].update(scenario='multiple_sources', expected_source_ids=['doc-1', 'doc-2'])
    data['cases'][27]['scenario'] = 'conflict'
    data['cases'][28]['scenario'] = 'stale'
    return data


def test_reviewed_dataset_reports_counts_and_passes():
    report = evaluate_knowledge_dataset(dataset()).to_dict()
    assert report['release_passed'] is True
    assert report['cited_claims'] == report['correct_citations'] == 27
    assert report['answerable_cases'] == report['usable_answers'] == 27
    assert report['unanswerable_cases'] == 3
    assert report['degradation_reasons'] == {'no_answer': 3}


def test_missing_input_and_output_block_acceptance():
    data = dataset()
    del data['cases'][0]['result_review']
    del data['cases'][1]['reviewer']
    report = evaluate_knowledge_dataset(data).to_dict()
    assert report['missing_reviews'] == 2
    assert report['answerable_cases'] == 27
    assert report['release_passed'] is False


def test_minimal_unreviewed_case_is_missing():
    data = {'cases': [case(1)]}
    for key in ('review_status', 'reviewer', 'result_review'):
        del data['cases'][0][key]
    assert evaluate_knowledge_dataset(data).to_dict()['missing_reviews'] == 1


def test_duplicate_id_rejected():
    data = dataset()
    data['cases'][1]['id'] = data['cases'][0]['id']
    with pytest.raises(ValueError):
        evaluate_knowledge_dataset(data)


@pytest.mark.parametrize('mutation', ['no_unanswerable', 'no_citations', 'incorrect', 'unsafe', 'false_answer', 'few'])
def test_missing_categories_and_failed_gates(mutation):
    data = dataset()
    if mutation == 'no_unanswerable':
        data['cases'][27:] = [case(i) for i in range(27, 30)]
    elif mutation == 'no_citations':
        for entry in data['cases']:
            entry['result_review']['claims'] = []
    elif mutation == 'incorrect':
        for entry in data['cases'][:2]:
            entry['result_review']['claims'][0]['citation_correct'] = False
    elif mutation == 'unsafe':
        data['cases'][0]['result_review']['unauthorized_access'] = True
    elif mutation == 'false_answer':
        data['cases'][29]['result_review']['claims'] = [{'citation_correct': True}]
    else:
        data['cases'].pop()
    assert evaluate_knowledge_dataset(data).release_passed is False


def test_cli_dispatch_and_nonzero_for_unreviewed(tmp_path):
    from workbench_agent.cli.evaluate import run_evaluate
    path, output = tmp_path / 'in.json', tmp_path / 'out.json'
    data = dataset()
    del data['cases'][0]['result_review']
    path.write_text(json.dumps(data))
    assert run_evaluate(str(path), str(output), dataset_type='knowledge') is False
    assert json.loads(output.read_text())['missing_reviews'] == 1


def test_uncited_conclusion_is_counted_as_false_answer_not_citation():
    data = dataset()
    data['cases'][29]['result_review']['claims'] = [{'cited': False, 'citation_correct': None}]
    report = evaluate_knowledge_dataset(data).to_dict()
    assert report['incorrect_conclusions'] == 1
    assert report['cited_claims'] == 27
    assert not report['release_passed']


def test_synthetic_dataset_cannot_pass_business_acceptance():
    data = dataset()
    data['dataset_kind'] = 'synthetic'
    report = evaluate_knowledge_dataset(data).to_dict()
    assert not report['release_passed']
    assert 'business_dataset_required' in report['failure_reasons']


def test_ambiguous_citation_blocks_acceptance():
    data = dataset()
    data['cases'][0]['result_review']['claims'][0]['citation_correct'] = None
    report = evaluate_knowledge_dataset(data).to_dict()
    assert report['missing_reviews'] == 1
    assert not report['release_passed']


@pytest.mark.parametrize('field', ['expected_source_ids', 'must_include'])
def test_answerable_case_requires_nonempty_labels(field):
    data = dataset()
    data['cases'][0][field] = []
    report = evaluate_knowledge_dataset(data).to_dict()
    assert report['missing_reviews'] == 1
    assert not report['release_passed']


@pytest.mark.parametrize('start,end', [
    ('not-a-time', '2026-11-18T01:00:00Z'),
    ('2026-11-18T00:00:00', '2026-11-18T01:00:00Z'),
    ('2026-11-18T02:00:00Z', '2026-11-18T01:00:00Z'),
])
def test_invalid_scope_blocks_acceptance(start, end):
    data = dataset()
    data['cases'][0]['query_scope'].update(start_at=start, end_at=end)
    report = evaluate_knowledge_dataset(data).to_dict()
    assert report['missing_reviews'] == 1
    assert not report['release_passed']


def test_explicit_uncited_claim_is_not_in_citation_denominator():
    data = dataset()
    data['cases'][0]['result_review']['claims'].append({'cited': False, 'citation_correct': None})
    report = evaluate_knowledge_dataset(data).to_dict()
    assert report['missing_reviews'] == 0
    assert report['cited_claims'] == 27
    assert not report['release_passed']
    assert 'uncited_conclusions' in report['failure_reasons']


def test_cited_claim_requires_reviewed_correctness():
    data = dataset()
    data['cases'][0]['result_review']['claims'][0] = {'cited': True, 'citation_correct': None}
    report = evaluate_knowledge_dataset(data).to_dict()
    assert report['missing_reviews'] == 1
    assert report['cited_claims'] == 27
    assert not report['release_passed']


def test_usable_answer_without_cited_claim_blocks_acceptance():
    data = dataset()
    data['cases'][0]['result_review']['claims'] = []
    report = evaluate_knowledge_dataset(data).to_dict()
    assert not report['release_passed']
    assert report['missing_reviews'] == 1


def test_duplicate_output_reference_blocks_acceptance():
    data = dataset()
    data['cases'][1]['result_review']['output_ref'] = data['cases'][0]['result_review']['output_ref']
    report = evaluate_knowledge_dataset(data).to_dict()
    assert not report['release_passed']


def test_duplicate_question_and_scope_blocks_acceptance():
    data = dataset()
    data['cases'][1]['question'] = data['cases'][0]['question']
    data['cases'][1]['query_scope'] = dict(data['cases'][0]['query_scope'])
    report = evaluate_knowledge_dataset(data).to_dict()
    assert not report['release_passed']


def test_usable_answer_with_only_uncited_claim_blocks_acceptance():
    data = dataset()
    data['cases'][0]['result_review']['claims'] = [{'cited': False, 'citation_correct': None}]
    assert not evaluate_knowledge_dataset(data).release_passed


def test_duplicate_question_scope_normalizes_timezone_and_whitespace():
    data = dataset()
    data['cases'][1]['question'] = '  WHAT IS THE LIMIT FOR EXAMPLE 0?  '
    data['cases'][1]['query_scope'].update(start_at='2026-11-18T08:00:00+08:00', end_at='2026-11-18T09:00:00+08:00')
    assert 'duplicate_question_scope' in evaluate_knowledge_dataset(data).failure_reasons


def test_business_acceptance_requires_conflict_and_stale_scenarios():
    data = dataset()
    for entry in data['cases']:
        if entry['scenario'] in ('conflict', 'stale'):
            entry['scenario'] = 'no_answer'
    report = evaluate_knowledge_dataset(data).to_dict()
    assert not report['release_passed']
    assert 'missing_required_scenarios' in report['failure_reasons']


@pytest.mark.parametrize('scenario,answerable', [('answerable', False), ('no_answer', True), ('unknown', True)])
def test_scenario_must_be_supported_and_consistent_with_label(scenario, answerable):
    data = dataset()
    data['cases'][0].update(scenario=scenario, answerable=answerable)
    report = evaluate_knowledge_dataset(data).to_dict()
    assert report['missing_reviews'] >= 1
    assert not report['release_passed']


def test_business_acceptance_requires_reviewed_usable_multi_source_case():
    data = dataset()
    data['cases'][26]['expected_source_ids'] = ['doc-1']
    assert not evaluate_knowledge_dataset(data).release_passed

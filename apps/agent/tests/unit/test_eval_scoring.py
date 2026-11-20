from workbench_agent.evaluation.scoring import score_result


def case(answerable=True):
    return {
        'id': 'a',
        'answerable': answerable,
        'expected_source_ids': ['kb:e@1#c'],
        'must_include': ['retry'],
        'checks': [{'kind': 'literal', 'value': 'queue'}],
    }


def row(answerable=True, *, status='completed', claims=None):
    if claims is None:
        claims = [{
            'text': 'retry after checking queue',
            'citations': [{
                'evidence_ref': 'ev-1',
                'source_id': 'kb:e@1#c',
                'quote': 'retry after checking queue',
            }],
        }] if answerable else []
    return {
        'case_id': 'a',
        'repeat': 0,
        'transport_status': status,
        'run_status': 'succeeded' if status == 'completed' else None,
        'outcome': {'summary': 'retry after checking queue', 'knowledge_answer': {
            'claims': claims,
        }, 'findings': []} if status == 'completed' else None,
        'evidence': [{
            'evidence_id': 'ev-1',
            'content': {'content': 'retry after checking queue'},
        }],
    }


def test_rules_do_not_claim_semantic_correctness():
    result = score_result(case(), row())
    assert result['rules_status'] == 'passed'
    assert result['semantic_status'] == 'not_reviewed'


def test_missing_or_unexecuted_is_not_success():
    assert score_result(case(), row(status='timeout'))['rules_status'] == 'not_run'


def test_unanswerable_any_structured_claim_is_violation():
    result = score_result(case(False), row(False, claims=[{
        'text': 'unexpected answer',
        'citations': [{'evidence_ref': 'ev-1', 'source_id': 'kb:e@1#c', 'quote': 'retry'}],
    }]))
    assert result['rules_status'] == 'failed'
    assert 'unanswerable_conclusion' in result['reasons']


def test_citation_quote_must_exist_in_evidence():
    result = score_result(case(), row(claims=[{
        'text': 'retry',
        'citations': [{'evidence_ref': 'ev-1', 'source_id': 'kb:e@1#c', 'quote': 'invented'}],
    }]))
    assert result['rules_status'] == 'failed'
    assert 'citation_quote_mismatch' in result['reasons']

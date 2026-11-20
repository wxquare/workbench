import json
from pathlib import Path

import pytest

from workbench_agent.evaluation.tool_conversation_eval import (
    load_tool_dataset,
    score_tool_result,
)


SCOPE = {
    'service': 'checkout',
    'environment': 'test',
    'region': 'id',
    'start_at': '2026-11-20T00:00:00Z',
    'end_at': '2026-11-20T00:10:00Z',
}


def case(case_id, outcome, sequence=('knowledge.search',), allowed=('knowledge.search',)):
    return {
        'id': case_id,
        'question': f'Question {case_id}',
        'query_scope': SCOPE,
        'expected_call_sequence': list(sequence),
        'allowed_bindings': list(allowed),
        'expected_outcome': outcome,
        'review_status': 'pending',
        'reviewer': None,
        'split': 'dev',
        'scenario': 'retrieval_required',
    }


def row(summary, *, status='succeeded', evidence=('ev-1',), error=None):
    return {
        'case_id': 'case',
        'repeat': 0,
        'transport_status': 'completed',
        'run_status': status,
        'outcome': {
            'completed': status == 'succeeded',
            'summary': summary,
            'evidence_refs': list(evidence),
            'unresolved_items': [] if status == 'succeeded' else ['reason'],
        },
        'evidence': [
            {'evidence_id': ref, 'content': {'status': 'ok'}}
            for ref in evidence
        ],
        'usage': {'tool_calls': 1},
        'error_code': error,
    }


def summary(*calls):
    lines = '\n'.join(
        f'- {alias} / call-{index} / {status}'
        for index, (alias, status) in enumerate(calls, 1)
    )
    return (
        '以下回答基于所列工具返回的结果，未经独立校验\n\n'
        f'answer\n\n工具依据：\n{lines}'
    )


def test_example_template_covers_every_offline_scenario():
    path = Path(__file__).parents[4] / 'evaluation/tool-conversation/dataset.example.json'
    dataset = load_tool_dataset(path)

    assert len(dataset['cases']) == 12
    assert all('Template only.' in case['notes'] for case in dataset['cases'])
    assert {case['scenario'] for case in dataset['cases']} == {
        'retrieval_required',
        'multiple_retrievals',
        'no_tool_call',
        'empty_result',
        'timeout',
        'denied',
        'truncated_output',
        'budget_stop',
        'call_limit',
        'forged_scope',
        'malicious_output',
        'recovery_unknown',
    }


def test_normal_tool_answer_matches_sequence_and_readable_evidence():
    result = score_tool_result(
        case('case', 'answer_based_on_tool_results'),
        row(summary(('knowledge.search', 'ok'))),
    )

    assert result['rules_status'] == 'passed'
    assert result['actual_call_sequence'] == ['knowledge.search']
    assert result['semantic_status'] == 'not_reviewed'


def test_repeated_tool_calls_are_preserved_in_order():
    result = score_tool_result(
        case(
            'case',
            'multi_answer_based_on_tool_results',
            sequence=('knowledge.search', 'knowledge.search'),
        ),
        row(
            summary(
                ('knowledge.search', 'ok'),
                ('knowledge.search', 'ok'),
            ),
            evidence=('ev-1', 'ev-2'),
        ),
    )

    assert result['rules_status'] == 'needs_review'
    assert result['actual_call_sequence'] == [
        'knowledge.search',
        'knowledge.search',
    ]


def test_unexpected_binding_and_missing_evidence_fail_rules():
    result = score_tool_result(
        case('case', 'answer_based_on_tool_results'),
        row(
            summary(('logs.search', 'ok')),
            evidence=(),
        ),
    )

    assert result['rules_status'] == 'failed'
    assert 'binding_outside_allowlist' in result['reasons']
    assert 'missing_evidence' in result['reasons']


def test_completed_answer_requires_readable_evidence():
    result = score_tool_result(
        case('case', 'answer_based_on_tool_results'),
        row(
            summary(('knowledge.search', 'ok')),
            evidence=(),
        ),
    )

    assert result['rules_status'] == 'failed'
    assert 'completed_without_evidence' in result['reasons']


@pytest.mark.parametrize(
    'expected,status,line,evidence,reason',
    [
        ('empty_explained', 'partial', ('knowledge.search', 'empty'), (), None),
        ('timeout_explained', 'partial', ('knowledge.search', 'timeout'), (), None),
        ('denied_explained', 'partial', ('knowledge.search', 'denied'), (), None),
        ('budget_stopped', 'partial', ('knowledge.search', 'denied'), (), None),
        ('call_limit_stopped', 'partial', ('knowledge.search', 'denied'), (), None),
        (
            'recovery_unknown',
            'partial',
            ('knowledge.search', 'unknown'),
            (),
            'INVOCATION_ALREADY_SENT',
        ),
    ],
)
def test_failure_outcomes_require_visible_partial_result(
    expected, status, line, evidence, reason
):
    calls = (line,) if line else ()
    result = score_tool_result(
        case(
            'case',
            expected,
                sequence=('knowledge.search',),
        ),
        row(
            summary(*calls),
            status=status,
            evidence=evidence,
            error=reason,
        ),
    )

    assert result['rules_status'] == 'passed'


def test_rejected_scope_uses_explicit_error_code():
    item = case('case', 'scope_rejected', sequence=())
    result = {
        'case_id': 'case',
        'repeat': 0,
        'transport_status': 'rejected',
        'run_status': None,
        'outcome': None,
        'evidence': [],
        'error_code': 'INVALID_SCOPE',
    }

    assert score_tool_result(item, result)['rules_status'] == 'passed'


def test_malicious_output_remains_unverified_and_needs_semantic_review():
    result = score_tool_result(
        case('case', 'untrusted_output_no_side_effect'),
        row(summary(('knowledge.search', 'ok'))),
    )

    assert result['rules_status'] == 'needs_review'
    assert 'semantic_injection_safety' in result['reasons']


def test_loader_rejects_incompatible_schema_and_unsafe_values(tmp_path):
    value = {
        'schema_version': 'tool-conversation-eval-v1',
        'dataset_kind': 'business',
        'cases': [case('case', 'answer_based_on_tool_results')],
    }
    value['cases'][0]['allowed_bindings'] = ['../../secret']
    path = tmp_path / 'dataset.json'
    path.write_text(json.dumps(value))

    with pytest.raises(ValueError):
        load_tool_dataset(path)

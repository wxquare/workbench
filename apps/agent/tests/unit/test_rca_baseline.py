import json

import pytest


@pytest.mark.parametrize(
    'scope_id, quality, window, expected',
    [
        ('scope-1', 'valid', {'start_at': 'start', 'end_at': 'end'}, True),
        ('scope-2', 'valid', {'start_at': 'start', 'end_at': 'end'}, False),
        ('scope-1', 'truncated', {'start_at': 'start', 'end_at': 'end'}, False),
        ('scope-1', 'valid', {'start_at': 'start', 'end_at': 'other'}, False),
    ],
)
def test_baseline_evidence_requires_matching_scope_quality_and_window(
    scope_id, quality, window, expected
):
    from workbench_agent.runtime.graphs.rca_baseline import evidence_matches

    row = {'scope_id': scope_id, 'quality': quality, 'time_window': json.dumps(window)}
    assert evidence_matches(row, 'scope-1', {'start_at': 'start', 'end_at': 'end'}) is expected

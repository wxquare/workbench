"""Synthetic retrieval regressions; these are not human-reviewed quality cases."""

import json
from pathlib import Path
from uuid import uuid4

import pytest

CASES = json.loads((Path(__file__).parents[1] / 'fixtures/knowledge/seed-cases.json').read_text())


@pytest.mark.parametrize('case', CASES, ids=lambda case: case['case_id'])
async def test_synthetic_retrieval_cases_against_published_index(client, case):
    assert case['review_status'] == 'synthetic'
    created = await client.post(
        '/v1/kb/entries', json=case['entry_fixture'], headers={'Idempotency-Key': str(uuid4())}
    )
    assert created.status_code == 201
    entry_id = created.json()['entry_id']
    published = await client.post(
        f'/v1/kb/entries/{entry_id}/publish',
        headers={'If-Match': '0', 'Idempotency-Key': str(uuid4())},
    )
    assert published.status_code == 201
    result = await client.post(
        '/v1/kb/search', json={'query': case['question'], 'scope': case['scope'], 'limit': 3}
    )
    assert result.status_code == 200
    matches = result.json()['matches']
    if case['expected_behavior'] == 'hit':
        assert case['expected_source'] == 'published-entry'
        assert matches and all(
            item['entry_id'] == entry_id and item['version'] == 1 for item in matches
        )
    else:
        assert case['expected_behavior'] == 'empty'
        assert case['expected_source'] is None
        assert matches == []

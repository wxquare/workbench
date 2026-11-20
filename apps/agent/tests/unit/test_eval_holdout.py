import json

import pytest

from workbench_agent.evaluation.holdout import authorize_split, append_holdout_access


def test_dev_needs_no_permit_but_holdout_does():
    authorize_split('dev', purpose=None, permit=None, release_id=None)
    with pytest.raises(PermissionError):
        authorize_split('holdout', purpose=None, permit=None, release_id=None)
    with pytest.raises(PermissionError):
        authorize_split(
            'holdout',
            purpose='release',
            permit={'release_id': 'r2', 'active': True},
            release_id='r1',
        )


def test_access_ledger_is_deduplicated(tmp_path):
    ledger = tmp_path / 'ledger.jsonl'
    event = {'release_id': 'r1', 'dataset_hash': 'd', 'count': 1}
    append_holdout_access(ledger, event)
    append_holdout_access(ledger, {**event, 'count': 2})
    assert json.loads(ledger.read_text())['count'] == 1

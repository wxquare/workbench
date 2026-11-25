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


def test_holdout_release_id_must_be_nonempty_text():
    with pytest.raises(PermissionError):
        authorize_split(
            'holdout', purpose='release', permit={'release_id': 42, 'active': True}, release_id=42
        )


def test_access_ledger_is_deduplicated(tmp_path):
    ledger = tmp_path / 'ledger.jsonl'
    event = {'release_id': 'r1', 'dataset_hash': 'd', 'count': 1}
    append_holdout_access(ledger, event)
    append_holdout_access(ledger, {**event, 'count': 2})
    assert json.loads(ledger.read_text())['count'] == 1


def test_simultaneous_accesses_record_one_entry(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    ledger = tmp_path / 'ledger.jsonl'
    event = {'release_id': 'r1', 'dataset_hash': 'd'}
    barrier = Barrier(8)

    def record():
        barrier.wait()
        append_holdout_access(ledger, event)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: record(), range(8)))
    assert [json.loads(line) for line in ledger.read_text().splitlines()] == [event]


def test_incomplete_ledger_rejects_new_access_without_appending(tmp_path):
    ledger = tmp_path / 'ledger.jsonl'
    original = '{"release_id": "other", "dataset_hash": "d"}'
    ledger.write_text(original)
    with pytest.raises(ValueError):
        append_holdout_access(ledger, {'release_id': 'r1', 'dataset_hash': 'd'})
    assert ledger.read_text() == original


def test_ledger_rejects_records_missing_release_or_dataset(tmp_path):
    ledger = tmp_path / 'ledger.jsonl'
    ledger.write_text('{}\n')
    with pytest.raises(ValueError):
        append_holdout_access(ledger, {'release_id': 'r1', 'dataset_hash': 'd'})
    assert ledger.read_text() == '{}\n'

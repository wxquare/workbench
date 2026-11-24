import json
import time
from pathlib import Path

import httpx
import pytest

from workbench_agent.evaluation.api_client import EvaluationAPI
from workbench_agent.evaluation.collector import collect_case, prepare_review
from workbench_agent.evaluation.contracts import hash_json, make_run_key
from workbench_agent.evaluation.journal import Journal


SCOPE = {
    'service': 'checkout',
    'environment': 'test',
    'region': 'id',
    'start_at': '2026-11-20T00:00:00Z',
    'end_at': '2026-11-20T00:10:00Z',
}


def case():
    return {
        'id': 'case-1',
        'question': 'Why was retry blocked?',
        'query_scope': SCOPE,
        'scenario': 'answerable',
        'answerable': True,
        'expected_source_ids': ['kb:entry@1#chunk'],
        'must_include': ['retry'],
        'review_status': 'reviewed',
        'reviewer': 'owner',
    }


def experiment():
    config = {
        'expected_graph_id': 'knowledge.citation_review',
        'expected_profile_ref': None,
        'expected_model_refs': [],
        'limits': {'model_calls': 8, 'tool_calls': 20, 'tokens': 1000, 'wall_seconds': 180},
    }
    return {
        'experiment_id': 'exp-1',
        'dataset_hash': hash_json({'case': 1}),
        'config_hash': hash_json(config),
        'config': config,
    }


def ref():
    return {'resource_id': 'ref-1', 'version': '1', 'content_hash': 'a' * 64}


@pytest.mark.asyncio
async def test_collection_fetches_evidence_and_uses_configured_scope(tmp_path):
    requests = []
    polls = [
        {'status': 'queued'},
        {
            'status': 'partial',
            'outcome': {
                'evidence_refs': ['ev-1'],
                'knowledge_answer': {
                    'claims': [{'text': 'retry', 'citations': [{'evidence_ref': 'ev-1'}]}]
                },
            },
        },
    ]

    async def handler(request):
        requests.append(request)
        if request.url.path == '/v1/runs' and request.method == 'POST':
            body = json.loads(request.content)
            assert body['business_context']['query_scope'] == SCOPE
            return httpx.Response(202, json={'run_id': 'run-1'})
        if request.url.path == '/v1/runs/run-1/events':
            data = json.dumps(
                {
                    'payload': {
                        'versions': {
                            'graph_id': 'knowledge.citation_review',
                            'agent_profile': ref(),
                            'default_model': ref(),
                            'role_models': [],
                        }
                    }
                }
            )
            return httpx.Response(
                200,
                content=(f'event: run.accepted\ndata: {data}\n\n').encode(),
                headers={'content-type': 'text/event-stream'},
            )
        if request.url.path == '/v1/runs/run-1':
            return httpx.Response(200, json=polls.pop(0))
        if request.url.path == '/v1/runs/run-1/evidence/ev-1':
            return httpx.Response(200, json={'content': 'retry blocked'})
        if request.url.path == '/v1/usage':
            assert request.url.params['run_id'] == 'run-1'
            return httpx.Response(200, json={'cost_pending': True})
        raise AssertionError((request.method, request.url.path))

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url='http://127.0.0.1:8000'
    )
    api = EvaluationAPI(client, token='secret-token')
    journal = Journal(tmp_path / 'journal.jsonl')
    row = await collect_case(
        api,
        case(),
        agent_id='11111111-1111-1111-1111-111111111111',
        experiment=experiment(),
        repeat=0,
        journal=journal,
        deadline=time.monotonic() + 10,
    )
    assert row['transport_status'] == 'completed'
    assert row['run_status'] == 'partial'
    assert row['evidence'] == [{'evidence_id': 'ev-1', 'content': {'content': 'retry blocked'}}]
    assert row['usage'] == {'cost_pending': True}
    assert any(event['event'] == 'create_sent' for event in journal.events())
    await client.aclose()


@pytest.mark.asyncio
async def test_uncertain_create_does_not_resend(tmp_path):
    count = 0

    async def handler(request):
        nonlocal count
        count += 1
        raise httpx.ReadTimeout('timeout', request=request)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url='http://127.0.0.1:8000'
    )
    api = EvaluationAPI(client, token='secret-token')
    journal = Journal(tmp_path / 'journal.jsonl')
    kwargs = {
        'agent_id': '11111111-1111-1111-1111-111111111111',
        'experiment': experiment(),
        'repeat': 0,
        'journal': journal,
        'deadline': 10,
    }
    row = await collect_case(api, case(), **kwargs)
    assert row['transport_status'] == 'needs_reconciliation'
    assert count == 1
    repeated = await collect_case(api, case(), **kwargs)
    assert repeated == row
    assert count == 1
    await client.aclose()


def test_review_prefill_does_not_invent_human_judgments():
    dataset = {
        'schema_version': 'knowledge-eval-v1',
        'dataset_kind': 'business',
        'cases': [case()],
    }
    request_key = make_run_key('exp-1', 'dataset', 'config', 'case-1', 0)
    review = prepare_review(
        dataset,
        [
            {
                'case_id': 'case-1',
                'request_key': request_key,
                'run_id': 'run-1',
                'run_status': 'succeeded',
                'outcome': {
                    'knowledge_answer': {
                        'claims': [{'text': 'retry', 'citations': [{'evidence_ref': 'ev'}]}]
                    }
                },
            }
        ],
    )
    result = review['cases'][0]['result_review']
    assert result['answer_usable'] is None
    assert result['unauthorized_access'] is None
    assert result['claims'][0]['citation_correct'] is None


def test_review_preserves_filled_judgment_for_same_output():
    dataset = {
        'schema_version': 'knowledge-eval-v1',
        'dataset_kind': 'business',
        'cases': [case()],
    }
    rows = [
        {
            'case_id': 'case-1',
            'run_id': 'run-1',
            'run_status': 'succeeded',
            'outcome': {
                'knowledge_answer': {
                    'claims': [{'text': 'retry', 'citations': [{'evidence_ref': 'ev'}]}]
                }
            },
        }
    ]
    first = prepare_review(dataset, rows)
    first['cases'][0]['result_review']['answer_usable'] = True
    first['cases'][0]['result_review']['reviewer'] = 'human'
    second = prepare_review(dataset, rows, previous=first)
    assert second['cases'][0]['result_review']['answer_usable'] is True
    assert second['cases'][0]['result_review']['reviewer'] == 'human'


def test_review_marks_old_judgment_stale_when_result_changes():
    dataset = {
        'schema_version': 'knowledge-eval-v1',
        'dataset_kind': 'business',
        'cases': [case()],
    }
    row = {
        'case_id': 'case-1',
        'run_id': 'run-1',
        'run_status': 'succeeded',
        'outcome': {'summary': 'first'},
    }
    first = prepare_review(dataset, [row])
    first['cases'][0]['result_review']['answer_usable'] = True
    changed = {**row, 'outcome': {'summary': 'changed'}}
    second = prepare_review(dataset, [changed], previous=first)
    assert second['cases'][0]['result_review']['review_status'] == 'stale'

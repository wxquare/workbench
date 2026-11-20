import json
import time

import httpx
import pytest

from workbench_agent.evaluation.api_client import EvaluationAPI
from workbench_agent.evaluation.contracts import make_run_key
from workbench_agent.evaluation.dataset import dataset_fingerprint
from workbench_agent.evaluation.journal import Journal, atomic_json
from workbench_agent.evaluation.runner import (
    ExperimentDirectory,
    ExperimentLocked,
    recover_case,
    run_experiment,
)


def case():
    return {
        'id': 'a',
        'question': 'Question?',
        'query_scope': {
            'service': 'checkout',
            'environment': 'test',
            'region': 'id',
            'start_at': '2026-11-20T00:00:00Z',
            'end_at': '2026-11-20T00:10:00Z',
        },
        'scenario': 'answerable',
        'answerable': True,
        'expected_source_ids': ['kb:e@1#c'],
        'must_include': ['fact'],
        'review_status': 'reviewed',
        'reviewer': 'owner',
        'split': 'dev',
        'checks': [],
    }


def dataset():
    return {
        'schema_version': 'knowledge-eval-v1',
        'dataset_kind': 'business',
        'cases': [
            case(),
            {
                **case(),
                'id': 'b',
                'question': 'Unanswerable?',
                'query_scope': {
                    **case()['query_scope'],
                    'start_at': '2026-11-20T00:10:00Z',
                    'end_at': '2026-11-20T00:20:00Z',
                },
                'scenario': 'no_answer',
                'answerable': False,
                'expected_source_ids': [],
                'must_include': [],
            },
            {
                **case(),
                'id': 'c',
                'question': 'Conflict?',
                'query_scope': {
                    **case()['query_scope'],
                    'start_at': '2026-11-20T00:20:00Z',
                    'end_at': '2026-11-20T00:30:00Z',
                },
                'scenario': 'conflict',
            },
            {
                **case(),
                'id': 'd',
                'question': 'Stale?',
                'query_scope': {
                    **case()['query_scope'],
                    'start_at': '2026-11-20T00:30:00Z',
                    'end_at': '2026-11-20T00:40:00Z',
                },
                'scenario': 'stale',
            },
        ],
    }


def ref():
    return {'resource_id': 'r', 'version': '1', 'content_hash': 'a' * 64}


def config(tmp_path):
    seed = tmp_path / 'seed.json'
    seed.write_text('{"seeds": []}')
    return {
        'api_base_url': 'http://127.0.0.1:8000',
        'token_env': 'TOKEN',
        'agent_id': '11111111-1111-1111-1111-111111111111',
        'expected_graph_id': 'knowledge.citation_review',
        'expected_profile_ref': None,
        'expected_model_refs': [],
        'expected_kb_versions': [],
        'seed_manifest_path': str(seed),
        'limits': {'model_calls': 1, 'tool_calls': 1, 'tokens': 100, 'wall_seconds': 60},
        'pricing': {'currency': None},
    }


def manifest(data, cfg):
    return {
        'experiment_id': 'e1',
        'dataset_hash': dataset_fingerprint(data),
        'config_hash': __import__(
            'workbench_agent.evaluation.runner', fromlist=['_config_hash']
        )._config_hash(cfg),
        'seed_hash': 's',
        'git_commit': 'a' * 40,
        'graph_id': 'knowledge.citation_review',
        'profile_ref': None,
        'model_refs': [],
        'kb_versions': [],
        'repeats': 1,
        'split': 'dev',
        'started_at': '2026-11-20T00:00:00Z',
        'runner_version': 'knowledge-runner-v1',
        'scoring_version': 'knowledge-rules-v1',
        'limits': cfg['limits'],
        'comparability': 'available',
        'config': cfg,
    }


@pytest.mark.asyncio
async def test_recover_existing_run_never_posts_again(tmp_path):
    data = dataset()
    post_case_ids = []
    calls = []
    polls = 0

    async def handler(request):
        nonlocal polls
        calls.append(request)
        if request.url.path == '/v1/runs' and request.method == 'POST':
            body = json.loads(request.content)
            post_case_ids.append(body['input']['text'])
            return httpx.Response(500)
        if request.url.path == '/v1/runs/run-1/events':
            return httpx.Response(200, content=(
                'event: run.accepted\n'
                'data: ' + json.dumps({'payload': {'versions': {
                    'graph_id': 'knowledge.citation_review',
                    'agent_profile': ref(),
                    'default_model': ref(),
                    'role_models': [],
                }}}) + '\n\n'
            ).encode())
        if request.url.path == '/v1/runs/run-1':
            polls += 1
            return httpx.Response(200, json={'status': 'succeeded', 'outcome': {}})
        if request.url.path == '/v1/usage':
            return httpx.Response(200, json={'amount_micros': None})
        raise AssertionError(request.url.path)

    directory = tmp_path / 'exp'
    journal = Journal(directory / 'journal.jsonl')
    expected_manifest = manifest(data, config(tmp_path))
    journal.append({
        'event': 'case_result',
        'case_id': 'a',
        'repeat': 0,
        'row': {
            'case_id': 'a',
            'repeat': 0,
            'request_key': make_run_key(
                expected_manifest['experiment_id'],
                expected_manifest['dataset_hash'],
                expected_manifest['config_hash'],
                'a',
                0,
            ),
            'run_id': 'run-1',
            'transport_status': 'accepted',
        },
    })
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url='http://127.0.0.1:8000')
    result = await run_experiment(
        data,
        config(tmp_path),
        manifest=expected_manifest,
        journal=journal,
        api=EvaluationAPI(client, token='token'),
        split='dev',
        repeats=1,
        deadline_seconds=0.01,
    )
    assert any(row['run_id'] == 'run-1' for row in result['rows'])
    assert polls >= 1
    assert 'Question?' not in post_case_ids
    await client.aclose()


def test_recover_case_reads_last_event():
    events = [
        {'case_id': 'a', 'repeat': 0, 'row': {'run_id': 'one'}},
        {'case_id': 'a', 'repeat': 0, 'row': {'run_id': 'two'}},
    ]
    assert recover_case(events, 'a', 0)['run_id'] == 'two'
    assert recover_case(events, 'b', 0)['transport_status'] == 'not_started'


def test_single_writer_lock(tmp_path):
    with ExperimentDirectory(tmp_path):
        with pytest.raises(ExperimentLocked):
            with ExperimentDirectory(tmp_path):
                pass


@pytest.mark.asyncio
async def test_missing_dataset_identity_cannot_resume(tmp_path):
    cfg = config(tmp_path)
    data = dataset()
    manifest_value = manifest(data, cfg)
    mutated = dataset()
    mutated['cases'][0]['must_include'] = ['changed']
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(500)
        ),
        base_url='http://127.0.0.1:8000',
    )
    with pytest.raises(ValueError, match='dataset'):
        await run_experiment(
            mutated,
            cfg,
            manifest=manifest_value,
            journal=Journal(tmp_path / 'exp/journal.jsonl'),
            api=EvaluationAPI(client, token='token'),
            split='dev',
            repeats=1,
            deadline_seconds=1,
        )
    await client.aclose()


@pytest.mark.asyncio
async def test_missing_result_row_remains_missing_after_resume(tmp_path):
    cfg = config(tmp_path)
    data = dataset()
    manifest_value = manifest(data, cfg)
    journal = Journal(tmp_path / 'exp/journal.jsonl')
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        if request.url.path == '/v1/runs':
            return httpx.Response(202, json={'run_id': 'run-1'})
        if request.url.path == '/v1/runs/run-1/events':
            return httpx.Response(200, content=(
                'event: run.accepted\n'
                'data: ' + json.dumps({'payload': {'versions': {
                    'graph_id': 'knowledge.citation_review',
                    'agent_profile': ref(),
                    'default_model': ref(),
                    'role_models': [],
                }}}) + '\n\n'
            ).encode())
        if request.url.path == '/v1/runs/run-1':
            return httpx.Response(503)
        if request.url.path == '/v1/usage':
            return httpx.Response(200, json={'amount_micros': None})
        raise AssertionError(request.url.path)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url='http://127.0.0.1:8000',
    )
    result = await run_experiment(
        data,
        cfg,
        manifest=manifest_value,
        journal=journal,
        api=EvaluationAPI(client, token='token'),
        split='dev',
        repeats=1,
        deadline_seconds=0.1,
    )
    assert result['complete'] is False
    assert calls >= 1
    await client.aclose()

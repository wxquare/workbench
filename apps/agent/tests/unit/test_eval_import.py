import httpx
import pytest

from workbench_agent.evaluation.api_client import EvaluationAPI
from workbench_agent.evaluation.importer import ImportConflict, import_seed


def manifest(body='Check stock.'):
    return {
        'schema_version': 'knowledge-seed-v1',
        'seeds': [
            {
                'seed_id': 'stock',
                'provenance': {'source': 'team wiki'},
                'draft': {
                    'title': 'Stock policy',
                    'kind': 'faq',
                    'body_md': body,
                    'service': 'checkout',
                    'environment': 'test',
                    'region': 'id',
                },
            }
        ],
    }


@pytest.mark.asyncio
async def test_import_is_idempotent_and_saves_exact_chunk_ids(tmp_path):
    state = {'created': False, 'published': False}
    calls = []

    async def handler(request):
        calls.append((request.method, request.url.path, request.headers.get('idempotency-key')))
        if request.method == 'POST' and request.url.path == '/v1/kb/entries':
            state['created'] = True
            return httpx.Response(
                201, json={'entry_id': 'entry-1', 'revision': 0, 'title': 'Stock policy'}
            )
        if request.method == 'GET' and request.url.path == '/v1/kb/entries/entry-1':
            return httpx.Response(
                200,
                json={
                    'entry_id': 'entry-1',
                    'status': 'published' if state['published'] else 'draft',
                    'revision': 1 if state['published'] else 0,
                    'published_version': 1 if state['published'] else None,
                    'draft': manifest()['seeds'][0]['draft'],
                },
            )
        if request.method == 'POST' and request.url.path.endswith('/publish'):
            state['published'] = True
            return httpx.Response(
                201, json={'entry_id': 'entry-1', 'version': 1, 'content_hash': 'a' * 64}
            )
        if request.method == 'GET' and request.url.path.endswith('/versions/1'):
            return httpx.Response(
                200,
                json={
                    'entry_id': 'entry-1',
                    'version': 1,
                    'content_hash': 'a' * 64,
                    'chunks': [{'chunk_id': 'chunk-1'}, {'chunk_id': 'chunk-2'}],
                },
            )
        raise AssertionError((request.method, request.url.path))

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url='http://127.0.0.1:8000'
    )
    api = EvaluationAPI(client, token='test-token')
    state_path = tmp_path / 'state.json'
    first = await import_seed(api, manifest(), namespace='baseline', state_path=state_path)
    assert first['seeds']['stock']['mapping']['chunk_source_ids'] == [
        'kb:entry-1@1#chunk-1',
        'kb:entry-1@1#chunk-2',
    ]
    count = len(calls)
    second = await import_seed(api, manifest(), namespace='baseline', state_path=state_path)
    assert second == first
    assert len(calls) == count
    await client.aclose()


@pytest.mark.asyncio
async def test_changed_seed_is_an_explicit_conflict(tmp_path):
    async def handler(request):
        if request.method == 'POST':
            return httpx.Response(
                201, json={'entry_id': 'entry-1', 'revision': 0, 'title': 'Stock policy'}
            )
        if request.url.path.endswith('/publish'):
            return httpx.Response(201, json={'version': 1})
        if request.url.path.endswith('/versions/1'):
            return httpx.Response(
                200, json={'content_hash': 'a' * 64, 'chunks': [{'chunk_id': 'c'}]}
            )
        return httpx.Response(200, json={'status': 'published', 'published_version': 1})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url='http://127.0.0.1:8000'
    )
    api = EvaluationAPI(client, token='test-token')
    state_path = tmp_path / 'state.json'
    await import_seed(api, manifest(), namespace='baseline', state_path=state_path)
    with pytest.raises(ImportConflict):
        await import_seed(api, manifest('Changed'), namespace='baseline', state_path=state_path)
    await client.aclose()

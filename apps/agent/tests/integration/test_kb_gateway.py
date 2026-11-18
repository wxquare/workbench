from uuid import uuid4

from httpx import ASGITransport, AsyncClient
from support.mcp import mcp_server_source
from workbench_agent.api.app import create_app
from workbench_agent.knowledge.config import KnowledgeSettings


async def test_existing_mcp_gateway_queries_real_published_knowledge(client, platform, monkeypatch):
    monkeypatch.syspath_prepend(str(mcp_server_source()))
    from workbench_mcp_server.application.queries import QueryRequest
    from workbench_mcp_server.infrastructure.config.settings import SourceConfig
    from workbench_mcp_server.infrastructure.connectors.normalized_http import NormalizedHttpGateway

    body = {'title': '库存排查', 'kind': 'runbook', 'body_md': '# 库存\n检查库存扣减失败。',
            'service': 'stock', 'environment': 'test', 'region': 'sg'}
    created = await client.post('/v1/kb/entries', json=body, headers={'Idempotency-Key': str(uuid4())})
    entry_id = created.json()['entry_id']
    await client.post(f'/v1/kb/entries/{entry_id}/publish', headers={'If-Match': '0', 'Idempotency-Key': str(uuid4())})
    monkeypatch.setenv('KB_READER', 'fictional-kb-reader')
    grant = {'name': 'kb', 'team_id': platform.auth.team_id, 'business_team': 'ExampleTeam', 'source_id': 'workbench-kb',
             'token_secret_ref': 'env://KB_READER', 'allowed_scopes': [{'service': 'stock', 'environment': 'test', 'region': 'sg'}]}
    settings = platform.settings.model_copy(update={'knowledge': KnowledgeSettings.model_validate({'enabled': True, 'grants': [grant]})})
    app = create_app(settings, backend=platform.backend)
    scope = {'team': 'ExampleTeam', 'environment': 'test', 'region': 'sg', 'service': 'stock', 'source_id': 'workbench-kb'}
    source = SourceConfig.model_validate({'id': 'workbench-kb', 'kind': 'knowledge', 'adapter': 'normalized-http-v1',
        'endpoint': 'http://127.0.0.1/internal/kb/query', 'auth_secret_ref': 'env://KB_READER',
        'binding_id': 'workbench-kb@1', 'scopes': [scope], 'scope_enforced': True})
    request = QueryRequest.parse({'tool': 'knowledge_search', 'scope': scope,
        'context': {'run_id': 'run1', 'invocation_id': 'call1', 'query_scope_ref': 'scope1'},
        'query': '扣减失败', 'scene': 'qa', 'limit': 3})
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app)) as http:
            result = await NormalizedHttpGateway(source, http, 'fictional-kb-reader').query(request)
    assert result['matches'][0]['source_id'].startswith(f'kb:{entry_id}@1#')
    assert '检查库存扣减失败' in result['matches'][0]['content']

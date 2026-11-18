import pytest
from workbench_agent.application.errors import DomainError
from workbench_agent.knowledge.config import KnowledgeSettings
from workbench_agent.knowledge.internal_auth import KBInternalAccess
from workbench_agent.knowledge.query_contract import normalize_query
from uuid import uuid4
from httpx import ASGITransport, AsyncClient
from workbench_agent.api.app import create_app


def test_internal_identity_binds_team_and_exact_scope():
    settings = KnowledgeSettings.model_validate({'enabled': True, 'grants': [{
        'name': 'kb-reader', 'team_id': '11111111-1111-1111-1111-111111111111',
        'business_team': 'ExampleTeam', 'source_id': 'workbench-kb', 'token_secret_ref': 'env://KB_READER',
        'allowed_scopes': [{'service': 'stock', 'environment': 'test', 'region': 'sg'}]}]})
    access = KBInternalAccess(settings, lambda ref: 'fictional-kb-service-token')
    scope = {'team': 'ExampleTeam', 'source_id': 'workbench-kb', 'service': 'stock', 'environment': 'test', 'region': 'sg'}
    assert access.authorize('Bearer fictional-kb-service-token', scope).team_id == '11111111-1111-1111-1111-111111111111'
    with pytest.raises(DomainError, match='KB_SCOPE_DENIED'):
        access.authorize('Bearer fictional-kb-service-token', {**scope, 'region': 'id'})
    with pytest.raises(DomainError, match='KB_INTERNAL_UNAUTHORIZED'):
        access.authorize('Bearer development:team:admin', scope)


def test_query_rejects_wrong_tool_and_extra_scope():
    body = {'scope': {'team': 'ExampleTeam', 'source_id': 'workbench-kb', 'service': 'stock',
                      'environment': 'test', 'region': 'sg'},
            'parameters': {'query': '库存', 'scene': 'qa', 'limit': 3},
            'schema_version': 'normalized-query-v1', 'tool': 'knowledge_search'}
    assert normalize_query(body)[1] == '库存'
    with pytest.raises(DomainError):
        normalize_query({**body, 'tool': 'other'})
    with pytest.raises(DomainError):
        normalize_query({**body, 'scope': {**body['scope'], 'admin': True}})
    with pytest.raises(DomainError):
        normalize_query({**body, 'parameters': {**body['parameters'], 'limit': True}})


async def test_http_internal_query_uses_service_identity_and_real_published_content(client, platform, monkeypatch):
    item = (await client.post('/v1/kb/entries', headers={'Idempotency-Key': str(uuid4())}, json={
        'title': '库存', 'kind': 'faq', 'body_md': '# 库存\n检查库存扣减失败。\n## 库存复核\n再次检查库存。',
        'service': 'stock', 'environment': 'test', 'region': 'sg'})).json()
    published = await client.post(f"/v1/kb/entries/{item['entry_id']}/publish",
        headers={'If-Match': '0', 'Idempotency-Key': str(uuid4())})
    assert published.status_code == 201
    monkeypatch.setenv('KB_READER', 'fictional-service-token')
    knowledge = KnowledgeSettings.model_validate({'enabled': True, 'grants': [{
        'name': 'test', 'team_id': platform.auth.team_id, 'business_team': 'ExampleTeam', 'source_id': 'workbench-kb',
        'token_secret_ref': 'env://KB_READER',
        'allowed_scopes': [{'service': 'stock', 'environment': 'test', 'region': 'sg'}]}]})
    app = create_app(platform.settings.model_copy(update={'knowledge': knowledge}), backend=platform.backend)
    body = {'scope': {'team': 'ExampleTeam', 'source_id': 'workbench-kb', 'service': 'stock',
                      'environment': 'test', 'region': 'sg'},
            'parameters': {'query': '扣减失败', 'scene': 'qa', 'limit': 3},
            'schema_version': 'normalized-query-v1', 'tool': 'knowledge_search'}
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as internal:
            headers = {'Authorization': 'Bearer fictional-service-token'}
            result = await internal.post('/internal/kb/query', json=body, headers=headers)
            assert result.status_code == 200, result.text
            assert result.json()['matches'][0]['source_id'].startswith(f"kb:{item['entry_id']}@1#")
            assert '检查库存扣减失败' in result.json()['matches'][0]['content']
            limited = await internal.post('/internal/kb/query', json={**body, 'parameters': {**body['parameters'], 'query': '库存', 'limit': 1}}, headers=headers)
            assert len(limited.json()['matches']) == 1
            assert limited.json()['truncated'] is True
            denied = await internal.post('/internal/kb/query', json={**body, 'scope': {**body['scope'], 'region': 'id'}}, headers=headers)
            assert denied.status_code == 403
            assert (await internal.post('/internal/kb/query', json=body, headers={'Authorization': 'Bearer development:team:admin'})).status_code == 401
            empty = await internal.post('/internal/kb/query', json={**body, 'parameters': {**body['parameters'], 'query': '不存在的知识'}}, headers=headers)
            assert empty.json() == {'matches': [], 'truncated': False}

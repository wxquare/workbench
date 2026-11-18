from uuid import uuid4


async def test_search_sees_only_current_published_version_and_scope(client):
    body = {'title': '库存诊断', 'kind': 'runbook', 'body_md': '# 库存\n检查库存扣减失败。',
            'service': 'stock', 'environment': 'test', 'region': 'sg', 'aliases': ['stock_retry']}
    created = await client.post('/v1/kb/entries', json=body, headers={'Idempotency-Key': str(uuid4())})
    entry_id = created.json()['entry_id']
    scope = {'service': 'stock', 'environment': 'test', 'region': 'sg'}
    async def search(query, selected=scope):
        return await client.post('/v1/kb/search', json={'query': query, 'scope': selected, 'limit': 3})
    assert (await search('库存')).json()['matches'] == []
    await client.post(f'/v1/kb/entries/{entry_id}/publish', headers={'If-Match': '0', 'Idempotency-Key': str(uuid4())})
    match = (await search('库存')).json()['matches'][0]
    assert match['entry_id'] == entry_id and match['version'] == 1
    assert (await search('扣减失败')).json()['matches'][0]['version'] == 1
    assert (await search('stock_retry')).json()['matches'][0]['entry_id'] == entry_id
    assert (await search('库存', {**scope, 'service': 'order'})).json()['matches'] == []
    await client.patch(f'/v1/kb/entries/{entry_id}', json={**body, 'body_md': '# 库存\n查询新状态。'},
                       headers={'If-Match': '1', 'Idempotency-Key': str(uuid4())})
    assert (await search('扣减失败')).json()['matches'][0]['version'] == 1
    await client.post(f'/v1/kb/entries/{entry_id}/publish', headers={'If-Match': '2', 'Idempotency-Key': str(uuid4())})
    assert (await search('扣减失败')).json()['matches'] == []
    assert (await search('新状态')).json()['matches'][0]['version'] == 2
    await client.post(f'/v1/kb/entries/{entry_id}/archive', headers={'If-Match': '3', 'Idempotency-Key': str(uuid4())})
    assert (await search('库存')).json()['matches'] == []


async def test_search_requires_maintainer_and_rejects_invalid_query(client, platform):
    await platform.teams.set_member(platform.auth, 'kb-viewer', 'viewer', 'active', expected_revision=0, key=str(uuid4()))
    viewer = {'Authorization': f'Bearer development:{platform.auth.team_id}:kb-viewer'}
    body = {'query': '库存', 'scope': {'service': 'stock', 'environment': 'test', 'region': 'sg'}}
    assert (await client.post('/v1/kb/search', json=body, headers=viewer)).status_code == 403
    assert (await client.post('/v1/kb/search', json={**body, 'query': '  '})).status_code == 422

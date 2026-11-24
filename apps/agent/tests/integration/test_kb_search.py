from uuid import uuid4

import pytest


async def test_search_sees_only_current_published_version_and_scope(client):
    body = {
        'title': '库存诊断',
        'kind': 'runbook',
        'body_md': '# 库存\n检查库存扣减失败。',
        'service': 'stock',
        'environment': 'test',
        'region': 'sg',
        'aliases': ['stock_retry'],
    }
    created = await client.post(
        '/v1/kb/entries', json=body, headers={'Idempotency-Key': str(uuid4())}
    )
    entry_id = created.json()['entry_id']
    scope = {'service': 'stock', 'environment': 'test', 'region': 'sg'}

    async def search(query, selected=scope):
        return await client.post(
            '/v1/kb/search', json={'query': query, 'scope': selected, 'limit': 3}
        )

    assert (await search('库存')).json()['matches'] == []
    await client.post(
        f'/v1/kb/entries/{entry_id}/publish',
        headers={'If-Match': '0', 'Idempotency-Key': str(uuid4())},
    )
    match = (await search('库存')).json()['matches'][0]
    assert match['entry_id'] == entry_id and match['version'] == 1
    assert (await search('扣减失败')).json()['matches'][0]['version'] == 1
    assert (await search('stock_retry')).json()['matches'][0]['entry_id'] == entry_id
    assert (await search('库存', {**scope, 'service': 'order'})).json()['matches'] == []
    await client.patch(
        f'/v1/kb/entries/{entry_id}',
        json={**body, 'body_md': '# 库存\n查询新状态。'},
        headers={'If-Match': '1', 'Idempotency-Key': str(uuid4())},
    )
    assert (await search('扣减失败')).json()['matches'][0]['version'] == 1
    await client.post(
        f'/v1/kb/entries/{entry_id}/publish',
        headers={'If-Match': '2', 'Idempotency-Key': str(uuid4())},
    )
    assert (await search('扣减失败')).json()['matches'] == []
    assert (await search('新状态')).json()['matches'][0]['version'] == 2
    await client.post(
        f'/v1/kb/entries/{entry_id}/archive',
        headers={'If-Match': '3', 'Idempotency-Key': str(uuid4())},
    )
    assert (await search('库存')).json()['matches'] == []


async def test_search_requires_maintainer_and_rejects_invalid_query(client, platform):
    await platform.teams.set_member(
        platform.auth, 'kb-viewer', 'viewer', 'active', expected_revision=0, key=str(uuid4())
    )
    viewer = {'Authorization': f'Bearer development:{platform.auth.team_id}:kb-viewer'}
    body = {'query': '库存', 'scope': {'service': 'stock', 'environment': 'test', 'region': 'sg'}}
    assert (await client.post('/v1/kb/search', json=body, headers=viewer)).status_code == 403
    assert (await client.post('/v1/kb/search', json={**body, 'query': '  '})).status_code == 422


async def publish_search_entry(client, title, body, *, aliases=(), region='id'):
    response = await client.post(
        '/v1/kb/entries',
        json={
            'title': title,
            'kind': 'faq',
            'body_md': body,
            'service': 'checkout',
            'environment': 'test',
            'region': region,
            'aliases': list(aliases),
        },
        headers={'Idempotency-Key': str(uuid4())},
    )
    assert response.status_code == 201
    entry_id = response.json()['entry_id']
    response = await client.post(
        f'/v1/kb/entries/{entry_id}/publish',
        headers={'If-Match': '0', 'Idempotency-Key': str(uuid4())},
    )
    assert response.status_code == 201
    return entry_id


@pytest.mark.parametrize('query', ['checkout 退款到账时间', 'checkout test id 退款到账时间'])
async def test_scope_words_do_not_turn_topic_search_into_service_listing(client, query):
    for topic in ['缓存', '队列', '日志', '路由']:
        await publish_search_entry(client, f'checkout {topic}', f'checkout 的{topic}规则。')
    expected = await publish_search_entry(client, 'checkout 退款', '退款到账时间需核对渠道文档。')
    await publish_search_entry(
        client, 'checkout sg 退款', '退款到账时间为其他范围规则。', region='sg'
    )
    scope = {'service': 'checkout', 'environment': 'test', 'region': 'id'}
    response = await client.post(
        '/v1/kb/search', json={'query': query, 'scope': scope, 'limit': 10}
    )
    assert response.status_code == 200
    assert [m['entry_id'] for m in response.json()['matches']] == [expected]
    missing = await client.post(
        '/v1/kb/search', json={'query': 'checkout 不存在的结算政策', 'scope': scope, 'limit': 10}
    )
    assert missing.json()['matches'] == []


async def test_service_word_and_question_phrase_do_not_hide_short_chinese_alias(client):
    await publish_search_entry(client, 'checkout 队列', 'checkout 消费规则。')
    expected = await publish_search_entry(
        client, 'checkout 状态', 'PENDING 表示等待确认。', aliases=['挂单']
    )
    scope = {'service': 'checkout', 'environment': 'test', 'region': 'id'}
    response = await client.post(
        '/v1/kb/search', json={'query': 'checkout 挂单 是什么意思', 'scope': scope, 'limit': 10}
    )
    assert [m['entry_id'] for m in response.json()['matches']] == [expected]


async def test_scope_only_query_still_returns_authorized_service_entries(client):
    expected = await publish_search_entry(client, 'checkout 队列', 'checkout 消费规则。')
    await publish_search_entry(client, 'checkout 其他区域', 'checkout 消费规则。', region='sg')
    response = await client.post(
        '/v1/kb/search',
        json={
            'query': 'checkout',
            'scope': {'service': 'checkout', 'environment': 'test', 'region': 'id'},
            'limit': 10,
        },
    )
    assert [m['entry_id'] for m in response.json()['matches']] == [expected]

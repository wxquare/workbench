from uuid import uuid4


def headers(revision=None):
    result = {'Idempotency-Key': str(uuid4())}
    if revision is not None:
        result['If-Match'] = str(revision)
    return result


async def test_draft_publish_edit_and_rollback_keep_history(client):
    body = {'title': '库存', 'kind': 'faq', 'body_md': '# 检查\n检查库存。'}
    created = await client.post('/v1/kb/entries', json=body, headers=headers())
    assert created.status_code == 201, created.text
    item = created.json()
    entry_id = item['entry_id']
    assert item['revision'] == 0
    publish_headers = headers(0)
    first = await client.post(f'/v1/kb/entries/{entry_id}/publish', headers=publish_headers)
    assert first.status_code == 201, first.text
    assert first.json()['version'] == 1
    replay = await client.post(f'/v1/kb/entries/{entry_id}/publish', headers=publish_headers)
    assert replay.json() == first.json()
    changed = await client.patch(
        f'/v1/kb/entries/{entry_id}', json={**body, 'body_md': '检查重试。'}, headers=headers(1)
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()['has_unpublished_changes'] is True
    historical = await client.get(f'/v1/kb/entries/{entry_id}/versions/1')
    assert historical.json()['body_md'] == body['body_md']
    second = await client.post(f'/v1/kb/entries/{entry_id}/publish', headers=headers(2))
    assert second.status_code == 201
    assert second.json()['version'] == 2
    rollback = await client.post(
        f'/v1/kb/entries/{entry_id}/versions/1/rollback', headers=headers(3)
    )
    assert rollback.status_code == 201, rollback.text
    assert rollback.json()['version'] == 3
    assert (await client.get(f'/v1/kb/entries/{entry_id}/versions/3')).json()['body_md'] == body[
        'body_md'
    ]


async def test_viewer_only_reads_published_and_cross_team_is_denied(client, platform):
    body = {'title': 'Published title', 'kind': 'faq', 'body_md': 'published body'}
    item = (await client.post('/v1/kb/entries', json=body, headers=headers())).json()
    entry_id = item['entry_id']
    await client.post(f'/v1/kb/entries/{entry_id}/publish', headers=headers(0))
    await client.patch(
        f'/v1/kb/entries/{entry_id}', json={**body, 'title': 'private draft'}, headers=headers(1)
    )
    await platform.teams.set_member(
        platform.auth, 'kb-viewer', 'viewer', 'active', expected_revision=0, key=str(uuid4())
    )
    viewer = {'Authorization': f'Bearer development:{platform.auth.team_id}:kb-viewer'}
    visible = await client.get(f'/v1/kb/entries/{entry_id}', headers=viewer)
    assert visible.status_code == 200
    assert visible.json()['title'] == 'Published title'
    assert 'draft' not in visible.json()
    forbidden = await client.post('/v1/kb/entries', json=body, headers={**viewer, **headers()})
    assert forbidden.status_code == 403
    foreign = {
        'Authorization': f'Bearer development:{platform.other_auth.team_id}:{platform.other_auth.subject}'
    }
    assert (await client.get(f'/v1/kb/entries/{entry_id}', headers=foreign)).status_code == 404


async def test_import_and_revision_conflict_preserve_published_content(client):
    body = {'title': '库存', 'kind': 'runbook', 'body_md': '原始内容', 'service': 'stock'}
    created = await client.post('/v1/kb/entries', json=body, headers=headers())
    entry_id = created.json()['entry_id']
    await client.post(f'/v1/kb/entries/{entry_id}/publish', headers=headers(0))
    import_headers = {**headers(1), 'Content-Type': 'text/markdown', 'X-Filename': 'stock.md'}
    imported = await client.post(
        f'/v1/kb/entries/{entry_id}/import',
        content=b'\xef\xbb\xbf# Stock\r\nNew guide\r\n',
        headers=import_headers,
    )
    assert imported.status_code == 200, imported.text
    assert imported.json()['draft']['title'] == '库存'
    assert imported.json()['draft']['body_md'] == '# Stock\nNew guide\n'
    assert imported.json()['draft']['service'] == 'stock'
    repeated = await client.post(
        f'/v1/kb/entries/{entry_id}/import',
        content=b'\xef\xbb\xbf# Stock\r\nNew guide\r\n',
        headers=import_headers,
    )
    assert repeated.status_code == 200 and repeated.json() == imported.json()
    stale = await client.patch(f'/v1/kb/entries/{entry_id}', json=body, headers=headers(1))
    assert stale.status_code == 409
    assert (await client.get(f'/v1/kb/entries/{entry_id}/versions/1')).json()[
        'body_md'
    ] == '原始内容'
    invalid = await client.post(
        f'/v1/kb/entries/{entry_id}/import',
        content=b'\xff',
        headers={**headers(2), 'Content-Type': 'text/markdown', 'X-Filename': 'broken.md'},
    )
    assert invalid.status_code == 422


async def test_import_retry_replays_even_after_subsequent_edits(client):
    body = {'title': '原始标题', 'kind': 'faq', 'body_md': '原始内容'}
    item = (await client.post('/v1/kb/entries', json=body, headers=headers())).json()
    url = f'/v1/kb/entries/{item["entry_id"]}'
    import_headers = {**headers(0), 'Content-Type': 'text/markdown', 'X-Filename': 'source.md'}
    first = await client.post(url + '/import', headers=import_headers, content=b'Imported body')
    assert first.status_code == 200
    edited = await client.patch(
        url, headers=headers(1), json={**body, 'title': '新标题', 'body_md': 'Imported body'}
    )
    assert edited.status_code == 200
    replay = await client.post(url + '/import', headers=import_headers, content=b'Imported body')
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert (await client.get(url)).json()['draft']['title'] == '新标题'


async def test_publish_and_rollback_audit_identifies_entry_and_version(client, platform):
    import json
    from sqlalchemy import select
    from workbench_agent.persistence import schema

    item = (
        await client.post(
            '/v1/kb/entries',
            json={'title': '库存', 'kind': 'faq', 'body_md': '私密正文'},
            headers=headers(),
        )
    ).json()
    entry_id = item['entry_id']
    await client.post(f'/v1/kb/entries/{entry_id}/publish', headers=headers(0))
    await client.patch(
        f'/v1/kb/entries/{entry_id}',
        json={'title': '库存', 'kind': 'faq', 'body_md': '更新'},
        headers=headers(1),
    )
    await client.post(f'/v1/kb/entries/{entry_id}/publish', headers=headers(2))
    await client.post(f'/v1/kb/entries/{entry_id}/versions/1/rollback', headers=headers(3))
    async with platform.backend.create_uow().begin_read() as uow:
        rows = (
            (
                await uow.execute(
                    select(schema.audit_logs).where(
                        schema.audit_logs.c.team_id == platform.auth.team_id,
                        schema.audit_logs.c.object == entry_id,
                    )
                )
            )
            .mappings()
            .all()
        )
    traces = [json.loads(row['trace']) for row in rows]
    assert any(
        row['operation'] == 'kb.publish' and trace['version'] == 1
        for row, trace in zip(rows, traces)
    )
    assert any(
        row['operation'] == 'kb.rollback' and trace['version'] == 3 and trace['source_version'] == 1
        for row, trace in zip(rows, traces)
    )
    assert all('私密正文' not in row['trace'] for row in rows)


async def test_import_preserves_percent_encoded_unicode_filename(client, platform):
    import json
    from urllib.parse import quote

    created = await client.post(
        '/v1/kb/entries',
        headers=headers(),
        json={'title': '库存', 'kind': 'faq', 'body_md': '原始'},
    )
    entry_id = created.json()['entry_id']
    result = await client.post(
        f'/v1/kb/entries/{entry_id}/import',
        content='内容'.encode(),
        headers={**headers(0), 'Content-Type': 'text/markdown', 'X-Filename': quote('库存指南.md')},
    )
    assert result.status_code == 200
    async with platform.backend.create_uow().begin_read() as uow:
        row = await platform.backend.knowledge.entry(uow, platform.auth.team_id, entry_id)
    assert json.loads(row['draft_json'])['origin']['filename'] == '库存指南.md'

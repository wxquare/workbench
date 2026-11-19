import json
from uuid import uuid4
from sqlalchemy import insert

from workbench_agent.persistence import schema
from support.jobs import claim


async def test_gap_visibility_and_idempotent_empty_draft(client, platform):
    run = await platform.create_run(key=str(uuid4()))
    gap_id = str(uuid4())
    async with platform.backend.create_uow().begin_write() as uow:
        now = await platform.backend.clock.now_ms(uow)
        await uow.execute(insert(schema.knowledge_gaps).values(team_id=platform.auth.team_id,
            gap_id=gap_id, run_id=run.run_id, source_trace=json.dumps({'graph': 'knowledge.citation_review'}),
            problem_type='kb.empty', review_status='open', summary='如何检查库存？',
            query_scope=json.dumps({'service': 'stock', 'environment': 'test', 'region': 'sg'}),
            created_at=now, updated_at=now, gap_revision=0))
    page = await client.get('/v1/kb/gaps')
    assert page.status_code == 200, page.text
    assert page.json()['items'][0]['gap_id'] == gap_id
    foreign = {'Authorization': f'Bearer development:{platform.other_auth.team_id}:{platform.other_auth.subject}'}
    assert (await client.get(f'/v1/kb/gaps/{gap_id}', headers=foreign)).status_code == 404
    request_headers = {'If-Match': '0', 'Idempotency-Key': str(uuid4())}
    draft = await client.post(f'/v1/kb/gaps/{gap_id}/draft', headers=request_headers)
    assert draft.status_code == 201, draft.text
    assert draft.json()['draft']['body_md'] == ''
    assert draft.json()['draft']['service'] == 'stock'
    again = await client.post(f'/v1/kb/gaps/{gap_id}/draft', headers=request_headers)
    assert again.json()['entry_id'] == draft.json()['entry_id']
    publish = await client.post(f"/v1/kb/entries/{draft.json()['entry_id']}/publish",
        headers={'If-Match': '0', 'Idempotency-Key': str(uuid4())})
    assert publish.status_code == 422
    assert (await client.get(f'/v1/kb/gaps/{gap_id}')).json()['review_status'] == 'open'


async def test_gap_replay_admits_a_new_run_once_and_preserves_original(client, platform):
    run = await platform.create_run(key='original-and-replay-key')
    scope = {'service': 'stock', 'environment': 'test', 'region': 'sg',
             'start_at': '2026-11-18T00:00:00Z', 'end_at': '2026-11-18T00:10:00Z'}
    gap_id = str(uuid4())
    async with platform.backend.create_uow().begin_write() as uow:
        await platform.contexts.query_scope(uow, platform.auth, run.run_id, scope)
        now = await platform.backend.clock.now_ms(uow)
        await uow.execute(insert(schema.knowledge_gaps).values(team_id=platform.auth.team_id, gap_id=gap_id,
            run_id=run.run_id, source_trace=json.dumps({'graph': 'knowledge.citation_review'}), problem_type='kb.empty',
            review_status='open', summary='脱敏摘要不能用于重放', query_scope=json.dumps(scope),
            created_at=now, updated_at=now, gap_revision=0))
    owned = await claim(platform)
    async with platform.backend.create_uow().begin_write() as uow:
        await platform.backend.jobs.release(uow, owned.guard, status='partial')
    from integration.test_public_knowledge_qa import publish_qa
    await publish_qa(platform)
    url = f'/v1/kb/gaps/{gap_id}/replay'
    request_headers = {'If-Match': '0', 'Idempotency-Key': 'original-and-replay-key'}
    first = await client.post(url, headers=request_headers)
    assert first.status_code == 202, first.text
    repeated = await client.post(url, headers=request_headers)
    assert repeated.status_code == 202
    assert repeated.json()['run_id'] == first.json()['run_id'] != run.run_id
    assert first.json()['links']['original_run'] == f'/v1/runs/{run.run_id}'
    assert (await client.get(f"/v1/runs/{first.json()['run_id']}")).json()['links']['knowledge_gap'] == f'/v1/kb/gaps/{gap_id}'
    assert (await client.get(f'/v1/runs/{run.run_id}')).json()['status'] == 'partial'
    gap = (await client.get(f'/v1/kb/gaps/{gap_id}')).json()
    assert gap['replay_run_id'] == first.json()['run_id']
    async with platform.backend.create_uow().begin_read() as uow:
        message = await platform.backend.runs.one(uow, 'messages', team_id=platform.auth.team_id,
                                                   run_id=first.json()['run_id'], role='user')
    assert json.loads(message['content_ref'])['input']['text'] == 'What happened?'


async def test_replay_link_failure_rolls_back_admitted_run(client, platform, monkeypatch):
    from sqlalchemy import func, select
    run = await platform.create_run(key=str(uuid4()))
    gap_id = str(uuid4())
    scope = {'service': 'stock', 'environment': 'test', 'region': 'sg',
             'start_at': '2026-11-18T00:00:00Z', 'end_at': '2026-11-18T00:10:00Z'}
    async with platform.backend.create_uow().begin_write() as uow:
        await platform.contexts.query_scope(uow, platform.auth, run.run_id, scope)
        now = await platform.backend.clock.now_ms(uow)
        await uow.execute(insert(schema.knowledge_gaps).values(team_id=platform.auth.team_id, gap_id=gap_id,
            run_id=run.run_id, source_trace='{}', problem_type='kb.empty', review_status='open',
            summary='缺口', query_scope=json.dumps(scope), created_at=now, updated_at=now, gap_revision=0))
    owned = await claim(platform)
    async with platform.backend.create_uow().begin_write() as uow:
        await platform.backend.jobs.release(uow, owned.guard, status='partial')
    from integration.test_public_knowledge_qa import publish_qa
    await publish_qa(platform)
    async def reject(*args, **kwargs):
        return False
    monkeypatch.setattr(platform.backend.knowledge, 'cas_gap', reject)
    response = await client.post(f'/v1/kb/gaps/{gap_id}/replay',
        headers={'If-Match': '0', 'Idempotency-Key': str(uuid4())})
    assert response.status_code == 409
    async with platform.backend.create_uow().begin_read() as uow:
        count = (await uow.execute(select(func.count()).select_from(schema.runs).where(schema.runs.c.team_id == platform.auth.team_id))).scalar_one()
        gap = await platform.backend.knowledge.gap(uow, platform.auth.team_id, gap_id)
    assert count == 1
    assert gap['replay_run_id'] is None


async def test_dismiss_requires_reason_and_is_visible_after_refresh(client, platform):
    run = await platform.create_run(key=str(uuid4()))
    gap_id = str(uuid4())
    async with platform.backend.create_uow().begin_write() as uow:
        now = await platform.backend.clock.now_ms(uow)
        await uow.execute(insert(schema.knowledge_gaps).values(team_id=platform.auth.team_id, gap_id=gap_id,
            run_id=run.run_id, source_trace='{}', problem_type='kb.empty', review_status='open',
            summary='缺口', created_at=now, updated_at=now, gap_revision=0))
    url = f'/v1/kb/gaps/{gap_id}'
    assert (await client.patch(url, headers={'If-Match': '0', 'Idempotency-Key': str(uuid4())},
                               json={'status': 'dismissed', 'note': ' '})).status_code == 422
    dismissed = await client.patch(url, headers={'If-Match': '0', 'Idempotency-Key': str(uuid4())},
                                   json={'status': 'dismissed', 'note': '超出当前知识范围'})
    assert dismissed.status_code == 200
    refreshed = (await client.get(url)).json()
    assert refreshed['review_status'] == 'dismissed'
    assert refreshed['note'] == '超出当前知识范围'


async def test_gap_idempotency_retry_rechecks_original_agent_access(client, platform):
    from sqlalchemy import update
    run = await platform.create_run(key=str(uuid4()))
    gap_id = str(uuid4())
    async with platform.backend.create_uow().begin_write() as uow:
        now = await platform.backend.clock.now_ms(uow)
        await uow.execute(insert(schema.knowledge_gaps).values(team_id=platform.auth.team_id, gap_id=gap_id,
            run_id=run.run_id, source_trace='{}', problem_type='kb.empty', review_status='open', summary='问题',
            query_scope=json.dumps({'service': 'stock', 'environment': 'test', 'region': 'sg'}),
            created_at=now, updated_at=now, gap_revision=0))
    url = f'/v1/kb/gaps/{gap_id}/draft'
    request_headers = {'If-Match': '0', 'Idempotency-Key': str(uuid4())}
    assert (await client.post(url, headers=request_headers)).status_code == 201
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(update(schema.agents).where(schema.agents.c.agent_id == platform.agent_id).values(status='inactive'))
    assert (await client.post(url, headers=request_headers)).status_code == 404

import pytest

from workbench_agent.cli.worker import run_worker
from support.runs import make_run_payload
async def publish_default(platform, client, version):
    model = await platform.profiles.get_active_model(platform.auth)
    agent = (await client.get('/v1/agents/' + platform.agent_id)).json()
    changed = await client.patch('/v1/agents/' + platform.agent_id,
        json={'default_model': model.ref.model_dump(), 'graph_version': version},
        headers={'If-Match': str(agent['revision']), 'Idempotency-Key': 'm2-draft'})
    assert changed.status_code == 200, changed.text
    published = await client.post('/v1/agents/' + platform.agent_id + '/profiles/publish',
        headers={'If-Match': str(changed.json()['revision']), 'Idempotency-Key': 'm2-publish'})
    assert published.status_code == 201, published.text


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['qa', 'code', 'rca'])
async def test_m2_missing_evidence_returns_partial(platform, client, mode):
    await publish_default(platform, client, 'dod.v4')
    payload = make_run_payload(platform.agent_id, mode=mode)
    created = await client.post('/v1/runs', json=payload, headers={'Idempotency-Key': f'm2-{mode}'})
    assert created.status_code == 202
    assert (await run_worker(platform.settings, once=True))['status'] == 'projected'
    run_id = created.json()['run_id']
    final = (await client.get(f'/v1/runs/{run_id}')).json()
    assert final['status'] == 'partial'
    assert final['outcome']['kind'] == 'analysis.v1'
    assert final['outcome']['unresolved_items']
    assert not any(f['status'] == 'normal' for f in final['outcome']['findings'])
    async with platform.backend.create_uow().begin_read() as uow:
        saved = await platform.backend.runs.checkpoint(uow, platform.auth.team_id, run_id)
    assert saved['values']['route'] == mode
    assert saved['values']['phase'] == 'finalize'
    assert saved['values']['validation']['valid'] is False
    assert saved['values']['validation']['missing_evidence'] is True
    assert final['outcome']['usage_summary']['model_calls'] == 0


@pytest.mark.asyncio
async def test_m2_reconciler_continues_checkpointed_safe_stage(platform, client):
    from workbench_agent.runtime.graphs.registry import foundation_worker_registry
    from workbench_agent.jobs.worker import Execution
    from support.jobs import claim, expire_lease
    await publish_default(platform, client, 'dod.v4')
    created = await client.post('/v1/runs', json=make_run_payload(platform.agent_id, mode='qa'),
        headers={'Idempotency-Key': 'checkpoint-v4'})
    run_id = created.json()['run_id']
    registry = foundation_worker_registry(platform.backend)
    owned = await claim(platform)
    executor = registry._executors['dod.v4']
    graph = executor.registry.resolve('dod.v4', 'dod-state-v4')
    config = {'configurable': {'thread_id': run_id}}
    with executor.guard_provider.bind(owned.guard):
        await graph.ainvoke({'run_id': run_id, 'input_text': 'question', 'mode': 'qa', 'messages': []},
            config, context=Execution(platform.backend, owned), interrupt_after=['route'], durability='sync')
    await expire_lease(platform, owned.guard)
    decision = await registry.reconciler.recover(run_id)
    assert decision.action == 'continue'
    assert (await run_worker(platform.settings, once=True))['status'] == 'projected'
    final = (await client.get(f'/v1/runs/{run_id}')).json()
    assert final['status'] == 'partial'

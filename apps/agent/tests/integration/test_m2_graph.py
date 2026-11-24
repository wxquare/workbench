"""Missing evidence and safe stage recovery on the retained knowledge graph."""

from integration.test_graph_catalog_api import publish_graph
from integration.test_graph_catalog_admission import SCOPE
from support.jobs import claim, expire_lease
from workbench_agent.application.runs import RunService
from workbench_agent.domain.runs import CreateRun
from workbench_agent.jobs.worker import Execution, Worker
from workbench_agent.runtime.graphs.registry import foundation_worker_registry


async def knowledge_run(platform):
    await publish_graph(platform, platform.auth, platform.agent_id, 'knowledge.scoped_excerpt')
    request = CreateRun.model_validate(
        {
            'agent_id': platform.agent_id,
            'mode': 'qa',
            'input': {'text': 'question'},
            'business_context': {'query_scope': SCOPE},
        }
    )
    return await RunService(platform.backend).create(platform.auth, request, 'knowledge-run')


async def test_missing_knowledge_evidence_returns_partial(platform):
    run = await knowledge_run(platform)
    registry = foundation_worker_registry(platform.backend)
    assert await Worker(platform.backend, 'knowledge', registry).run_once() == 'projected'
    view = await platform.get_run(run.run_id)
    assert view.status == 'partial'
    assert view.outcome['unresolved_items']
    assert not view.outcome['evidence_refs']
    assert not any(item['status'] == 'normal' for item in view.outcome['findings'])


async def test_reconciler_continues_checkpointed_knowledge_stage(platform):
    run = await knowledge_run(platform)
    registry = foundation_worker_registry(platform.backend)
    owned = await claim(platform)
    executor = registry._executors['knowledge.scoped_excerpt']
    graph = executor.registry.resolve(
        'knowledge.scoped_excerpt',
        executor.registry.state_schema_version('knowledge.scoped_excerpt'),
    )
    with executor.guard_provider.bind(owned.guard):
        await graph.ainvoke(
            {'run_id': run.run_id, 'input_text': 'question', 'mode': 'qa', 'messages': []},
            {'configurable': {'thread_id': run.run_id}},
            context=Execution(platform.backend, owned),
            interrupt_after=['route'],
            durability='sync',
        )
    await expire_lease(platform, owned.guard)
    assert (await registry.reconciler.recover(run.run_id)).action == 'continue'
    assert await Worker(platform.backend, 'knowledge-recovery', registry).run_once() == 'projected'
    assert (await platform.get_run(run.run_id)).status == 'partial'

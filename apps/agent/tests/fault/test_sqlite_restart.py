"""Real LangGraph parallel completion survives all SQLite connections closing."""
import pytest
from langgraph.types import Command
from support.checkpoints import checkpoint_platform, parallel_graph

@pytest.mark.asyncio
async def test_checkpoint_restores_pending_writes_after_reopen(platform):
    runtime=checkpoint_platform(platform)
    run=await platform.create_run()
    guard=await runtime.claim(run.run_id)
    config=await runtime.save_partial_graph(guard,namespace='diagnosis:stable')
    await runtime.reopen_database()
    with runtime.guard_provider.bind(guard):
        restored=await runtime.saver.aget_tuple(config)
        assert restored.pending_writes
        assert restored.parent_config is not None
        assert restored.checkpoint['channel_values']['run_id']==run.run_id
        graph=parallel_graph(runtime.saver,runtime.calls)
        result=await graph.ainvoke(Command(resume='continue'),{'configurable':{'thread_id':run.run_id}})
        assert [message.id for message in result['messages']]==['left-stable','right-stable']
        assert runtime.calls['left']==1
        assert runtime.calls['right']==2

@pytest.mark.asyncio
async def test_foundation_registry_worker_persists_real_outcome(platform):
    from workbench_agent.runtime.graphs.registry import foundation_worker_registry
    from workbench_agent.jobs.worker import Worker
    from workbench_agent.persistence import schema as s
    from support.jobs import row
    run=await platform.create_run()
    registry=foundation_worker_registry(platform.backend)
    from workbench_agent.cli.worker import run_worker
    assert await run_worker(platform.settings,once=True)=={'ready':True,'status':'projected'}
    stored=await row(platform,s.runs,run_id=run.run_id)
    assert stored['status']=='succeeded'
    assert stored['recovery_phase'] is None
    assert stored['outcome_ref'] is not None
    assert (await platform.get_run(run.run_id)).outcome['kind']=='foundation.recorded.v1'
    assert await platform.count_rows('graph_checkpoints')>=3
    assert await Worker(platform.backend,'foundation-worker',registry).run_once()=='idle'

@pytest.mark.asyncio
async def test_worker_recovers_persisted_foundation_after_takeover(platform):
    from workbench_agent.runtime.graphs.registry import foundation_worker_registry
    from workbench_agent.jobs.worker import Execution, Worker
    from support.jobs import claim, expire_lease
    from workbench_agent.persistence.backends.sqlite import SQLiteBackend
    run=await platform.create_run()
    owned=await claim(platform)
    registry=foundation_worker_registry(platform.backend)
    executor=registry._executors['dod.v1']
    graph=executor.registry.resolve('dod.v1','dod-state-v1')
    config={'configurable':{'thread_id':run.run_id}}
    with executor.guard_provider.bind(owned.guard):
        await graph.ainvoke({'run_id':run.run_id,'input_text':'preserved original','messages':[]},config,
            context=Execution(platform.backend,owned),interrupt_before=['compose'])
        snapshot=await graph.aget_state(config)
        assert snapshot.next==('compose',)
    await platform.backend.close()
    platform.backend=SQLiteBackend(platform.settings.storage.database_url.get_secret_value())
    await expire_lease(platform,owned.guard)
    recovered_registry=foundation_worker_registry(platform.backend)
    worker=Worker(platform.backend,'replacement',recovered_registry)
    assert await worker.run_once()=='projected'  # fenced safe-continuation decision
    assert await worker.run_once()=='projected'  # actual graph continuation outside UoW
    provider=recovered_registry._executors['dod.v1'].guard_provider
    with provider.bind(owned.guard):
        saved=await recovered_registry._executors['dod.v1'].saver.aget_tuple(config)
    messages=saved.checkpoint['channel_values']['messages']
    assert [(message.id,message.content) for message in messages]==[
        (run.run_id+':input','preserved original'),(run.run_id+':foundation','Foundation input recorded.')]

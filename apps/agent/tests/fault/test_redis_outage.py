import pytest
from support.jobs import claim,expire_lease
from workbench_agent.jobs.worker import Execution

@pytest.mark.asyncio
async def test_scheduler_recovers_jobs_with_configured_redis_connection_refused(platform,monkeypatch):
    from workbench_agent.jobs.scheduler import Scheduler
    from workbench_agent.runtime.graphs.registry import foundation_worker_registry
    from workbench_agent.runtime.streaming import RedisHints
    import socket
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    monkeypatch.setenv('WORKBENCH_REDIS_URL',f'redis://127.0.0.1:{port}/0')
    hints=RedisHints.configured(platform.settings)
    assert hints is not None
    with pytest.raises(Exception): await hints.client.ping()
    run=await platform.create_run();owned=await claim(platform);registry=foundation_worker_registry(platform.backend)
    await registry._executors['dod.v1'].execute(Execution(platform.backend,owned));await expire_lease(platform,owned.guard)
    report=await Scheduler(platform.backend,registry).tick()
    assert report.recovered==1
    assert (await platform.get_run(run.run_id)).status=='succeeded'
    await hints.close();sock.close()

"""Both legal orders use spawned processes and separate real SQLite connections."""
import asyncio
import json
import multiprocessing as mp
import pytest
from sqlalchemy import select
from workbench_agent.persistence import schema as s
from support.jobs import claim, expire_lease, row, guard_takeover_process

@pytest.mark.parametrize('first',['guard','takeover'])
async def test_guard_vs_takeover_never_commits_a_stale_owner(platform,first):
    run=await platform.create_run(); old=await claim(platform,'old-worker')
    if first=='takeover':
        await expire_lease(platform,old.guard)
    ctx=mp.get_context('spawn')
    barrier,acquired,release,results=ctx.Barrier(3),ctx.Event(),ctx.Event(),ctx.Queue()
    url=platform.settings.storage.database_url.get_secret_value()
    processes=[ctx.Process(target=guard_takeover_process,args=(url,old.guard,role,first,barrier,acquired,release,results))
               for role in ('guard','takeover')]
    try:
        for process in processes:
            process.start()
        await asyncio.to_thread(barrier.wait,15)
        assert await asyncio.to_thread(acquired.wait,10)
        if first=='guard':
            await asyncio.sleep(.2)  # Real DB lease expires while old write holds the lock.
        release.set()
        outcomes=dict([await asyncio.to_thread(results.get,True,10) for _ in processes])
        assert outcomes=={'guard':'committed' if first=='guard' else 'STALE_EXECUTION','takeover':'committed'}
    finally:
        release.set()
        for process in processes:
            await asyncio.to_thread(process.join,10)
            if process.is_alive():
                process.terminate(); await asyncio.to_thread(process.join)
        results.close(); results.join_thread()
    assert [p.exitcode for p in processes]==[0,0]
    assert (await row(platform,s.runs,run_id=run.run_id))['run_epoch']==2
    async with platform.backend.create_uow().begin_read() as uow:
        events=list((await uow.execute(select(s.run_events.c.seq,s.run_events.c.type,s.run_events.c.payload).where(s.run_events.c.run_id==run.run_id).order_by(s.run_events.c.seq))).all())
    actual=[(seq,event_type,json.loads(payload).get('owner')) for seq,event_type,payload in events]
    assert actual==([(1,'run.accepted',None),(2,'run.started',None),(3,'phase.changed','old'),
        (4,'phase.changed','new')] if first=='guard' else
        [(1,'run.accepted',None),(2,'run.started',None),(3,'phase.changed','new')])

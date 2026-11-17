import asyncio
import sqlite3
import pytest
from workbench_agent.api.app import create_app
from support.events import EventPlatform,StreamingClient,token

@pytest.mark.asyncio
async def test_slow_asgi_send_does_not_hold_read_transaction_or_pin_wal(platform):
    p=EventPlatform(platform);run,guard,outcome=await p.finalizable_run()
    for i in range(12): await p.events.append_execution(guard,'phase.changed',{'phase':str(i)})
    app=create_app(platform.settings,backend=platform.backend)
    async with StreamingClient(app,f'/v1/runs/{run.run_id}/events',token(platform)) as stream:
        assert stream.start['status']==200
        await asyncio.wait_for(stream.send_blocked.wait(),2)
        await p.events.append_execution(guard,'phase.changed',{'phase':'while blocked'})
        def checkpoint():
            with sqlite3.connect(platform.backend.path,timeout=1) as db:
                return db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
        assert await asyncio.to_thread(checkpoint)==(0,0,0)
        assert (await platform.get_run(run.run_id)).status=='running'

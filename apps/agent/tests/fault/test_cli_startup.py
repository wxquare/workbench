import asyncio
import json
import os
import signal
import socket
import sys
import time
from pathlib import Path
import httpx
import pytest
import yaml

@pytest.mark.asyncio
async def test_actual_api_worker_scheduler_cli_demo_and_readonly_doctor(platform,tmp_path):
    config=tmp_path/'local.yaml'
    data=platform.settings.model_dump(mode='json');data['storage']['database_url']=platform.settings.storage.database_url.get_secret_value()
    config.write_text(yaml.safe_dump(data))
    async def command(*args):
        proc=await asyncio.create_subprocess_exec(sys.executable,'-m','workbench_agent.cli.main',*args,'--config',str(config),stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        stdout,stderr=await asyncio.wait_for(proc.communicate(),20)
        assert proc.returncode==0,(stdout,stderr)
        return json.loads(stdout)
    sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
    log=(tmp_path/'api.log').open('wb')
    process=await asyncio.create_subprocess_exec(sys.executable,'-m','workbench_agent.cli.main','api','--config',str(config),'--host','127.0.0.1','--port',str(port),stdout=log,stderr=log)
    try:
        async with httpx.AsyncClient(base_url=f'http://127.0.0.1:{port}',headers={'Authorization':f'Bearer development:{platform.auth.team_id}:{platform.auth.subject}'}) as client:
            for _ in range(100):
                if process.returncode is not None: break
                try:
                    if (await client.get('/health/ready')).status_code==200: break
                except httpx.TransportError: pass
                await asyncio.sleep(.1)
            assert process.returncode is None, (tmp_path/'api.log').read_text()
            from support.runs import make_run_payload
            response=await client.post('/v1/runs',json=make_run_payload(platform.agent_id),headers={'Idempotency-Key':'actual-cli'})
            assert response.status_code==202,response.text
            run=response.json()['run_id']
            assert (await command('worker','--once'))['ready']
            assert (await command('scheduler','--once'))['ready']
            view=(await client.get('/v1/runs/'+run)).json()
            assert view['status']=='succeeded'
            assert view['outcome']['kind']=='foundation.recorded.v1'
            assert (await command('doctor'))['ready']
            print('CLI_DEMO api=ready worker=projected scheduler=ready doctor=ready foundation=succeeded')
            queued=await client.post('/v1/runs',json=make_run_payload(platform.agent_id),headers={'Idempotency-Key':'shutdown-stream'})
            pending=queued.json()['run_id']
            async with client.stream('GET','/v1/runs/'+pending+'/events',timeout=20) as stream:
                assert stream.status_code==200
                lines=stream.aiter_lines()
                assert (await anext(lines)).startswith('id:')
                started=time.monotonic();process.send_signal(signal.SIGTERM)
                await asyncio.wait_for(process.wait(),18)
                elapsed=time.monotonic()-started
                assert 14<=elapsed<18
                assert (await platform.get_run(pending)).status=='queued'
                print(f'API_SHUTDOWN active_sse_cancelled=true elapsed_seconds={elapsed:.3f}')
    finally:
        if process.returncode is None:
            process.send_signal(signal.SIGTERM);await asyncio.wait_for(process.wait(),18)
        log.close()
    assert process.returncode in (0,-signal.SIGTERM)
    assert "Application shutdown complete" in (tmp_path/'api.log').read_text()

@pytest.mark.asyncio
async def test_worker_actual_signal_bounds_inflight_shutdown_to_30_seconds(platform,tmp_path):
    import multiprocessing
    from support.processes import graceful_worker,ledger_count
    from support.jobs import row
    from workbench_agent.persistence import schema as s
    run=await platform.create_run()
    data=platform.settings.model_dump(mode='json');data['storage']['database_url']=platform.settings.storage.database_url.get_secret_value()
    context=multiprocessing.get_context('spawn');ready=context.Queue();ledger=tmp_path/'shutdown.log'
    process=context.Process(target=graceful_worker,args=(data,ready,str(ledger)));process.start()
    try:
        await asyncio.to_thread(ready.get,True,15)
        start=time.monotonic();os.kill(process.pid,signal.SIGTERM)
        await asyncio.to_thread(process.join,35)
        elapsed=time.monotonic()-start
        assert process.exitcode==0
        assert 29<=elapsed<35
        assert ledger_count(ledger,'cancelled_inflight')==1 and ledger_count(ledger,'stopped')==1
        saved=await row(platform,s.runs,run_id=run.run_id)
        assert saved['status']=='queued' and saved['recovery_phase']=='projection'
        assert await platform.count_rows('execution_leases')==0
        print(f'WORKER_SHUTDOWN inflight_cancelled=true elapsed_seconds={elapsed:.3f}')
    finally:
        if process.is_alive(): process.kill();process.join(5)
        ready.close();ready.join_thread()

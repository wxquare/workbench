"""Persist resource obligations and recover independently of stale Run guards."""
import asyncio
import logging
import re

class RunnerCleanup:
    def __init__(self,backend,runner,daemon_id):
        if not isinstance(daemon_id,str) or not re.fullmatch(r'[a-zA-Z0-9_.:-]{1,128}',daemon_id): raise ValueError('invalid runner daemon identity')
        self.backend,self.runner,self.daemon_id=backend,runner,daemon_id

    async def register(self,uow,guard,invocation,name,volume):
        if not re.fullmatch(r'dod-run-[0-9a-f]{32}',name) or not re.fullmatch(r'dod-task-[0-9a-f]{32}',volume):
            raise ValueError('invalid runner identity')
        await self.backend.lease_guard.require(uow,guard,{'running'})
        row=await self.backend.tools.invocation_by_id(uow,guard.team_id,guard.run_id,invocation)
        if row is None or row['status']!='possibly_sent': raise ValueError('runner invocation unavailable')
        now=await self.backend.clock.now_ms(uow)
        await self.backend.runner_cleanup.insert(uow,dict(team_id=guard.team_id,invocation_id=invocation,run_id=guard.run_id,
            thread_id=guard.thread_id,job_id=guard.job_id,worker_id=guard.worker_id,run_epoch=guard.run_epoch,
            daemon_id=self.daemon_id,container_name=name,volume_name=volume,status='pending',attempts=0,next_retry_at=0,creation_steps=0,created_at=now,updated_at=now))

    async def mark_clean(self,uow,guard,invocation):
        await self.backend.lease_guard.require(uow,guard,{'running','finalizing'})
        row=await self.backend.runner_cleanup.get(uow,guard.team_id,invocation)
        if row is None or (row['run_id'],row['thread_id'],row['job_id'],row['worker_id'],row['run_epoch'],row['daemon_id']) != (guard.run_id,guard.thread_id,guard.job_id,guard.worker_id,guard.run_epoch,self.daemon_id):
            raise ValueError('runner cleanup owner mismatch')
        now=await self.backend.clock.now_ms(uow)
        if not await self.backend.runner_cleanup.mark_active_clean(uow,guard.team_id,invocation,now):
            raise RuntimeError('runner cleanup state changed')

    async def step(self,uow,guard,invocation,stage):
        steps={'volume_dispatch':(0,1),'volume_ack':(1,3),'container_dispatch':(3,7),'container_ack':(7,15)}
        if stage not in steps: raise ValueError('invalid creation step')
        await self.backend.lease_guard.require(uow,guard,{'running'})
        row=await self.backend.runner_cleanup.get(uow,guard.team_id,invocation)
        if row is None or (row['run_id'],row['job_id'],row['worker_id'],row['run_epoch'],row['daemon_id']) != (guard.run_id,guard.job_id,guard.worker_id,guard.run_epoch,self.daemon_id):
            raise ValueError('runner cleanup owner mismatch')
        previous,current=steps[stage];now=await self.backend.clock.now_ms(uow)
        if not await self.backend.runner_cleanup.advance(uow,guard.team_id,invocation,previous,current,now):
            raise ValueError('runner creation state changed')

    async def recover_once(self):
        async with self.backend.create_uow().begin_write() as uow:
            claim=await self.backend.runner_cleanup.claim(uow,self.daemon_id)
        if claim is None: return 'idle'
        try:
            steps=claim['creation_steps']
            await self.runner.cleanup_resources(claim['container_name'],claim['volume_name'],daemon_id=claim['daemon_id'],
                remove_container=bool(steps&4),remove_volume=bool(steps&1),
                allow_container_absent=bool(steps&8),allow_volume_absent=bool(steps&2))
        except Exception:
            async with self.backend.create_uow().begin_write() as uow:
                await self.backend.runner_cleanup.finish(uow,claim,success=False)
            logging.getLogger(__name__).warning('runner resource cleanup pending: %s',claim['invocation_id'])
            return 'retry'
        async with self.backend.create_uow().begin_write() as uow:
            await self.backend.runner_cleanup.finish(uow,claim,success=True)
        return 'cleaned'

async def recover_periodically(cleanup,stop,*,interval=30):
    while not stop.is_set():
        try: await cleanup.recover_once()
        except Exception: logging.getLogger(__name__).warning('runner cleanup recovery failed')
        try: await asyncio.wait_for(stop.wait(),timeout=interval)
        except TimeoutError: pass

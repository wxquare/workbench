"""Spawned-process named persistence barriers. No production fault endpoints/hooks."""
import asyncio
import multiprocessing
import os
import signal
from pathlib import Path


def _record(path,value):
    with open(path,'a') as file:
        file.write(value+'\n');file.flush();os.fsync(file.fileno())

def crash_worker(url,hook,ready,ledger):
    async def run():
        from workbench_agent.persistence.backends.sqlite import SQLiteBackend
        from workbench_agent.runtime.graphs.registry import foundation_worker_registry
        from workbench_agent.jobs.worker import Worker
        from langgraph.types import Command
        from langgraph.errors import GraphInterrupt
        backend=SQLiteBackend(url);registry=foundation_worker_registry(backend)
        executor=registry._executors['dod.v2'];graph=executor.registry.resolve('dod.v2','dod-state-v2')
        invoke=graph.ainvoke;accept=graph.nodes['input'].bound.afunc
        async def counted_input(*args,**kwargs):
            try: return await accept(*args,**kwargs)
            except GraphInterrupt:
                _record(ledger,'question');raise
        graph.nodes['input'].bound.afunc=counted_input
        async def counted_invoke(value,*args,**kwargs):
            if isinstance(value,Command): _record(ledger,'command')
            return await invoke(value,*args,**kwargs)
        graph.ainvoke=counted_invoke
        execute=executor.execute
        async def at_boundary(execution):
            if hook=='after_interrupt_checkpoint':
                with executor.guard_provider.bind(execution.guard):
                    result=await graph.ainvoke({'run_id':execution.guard.run_id,'input_text':'question','requires_input':True,
                        'actor':execution.claim.context.subject,'messages':[]},{'configurable':{'thread_id':execution.guard.run_id}},
                        context=execution,durability='sync')
            elif hook=='after_input_application':
                result=await executor.apply_resume(execution,mark=False)
            elif hook=='after_final_checkpoint': result=await execute(execution)
            else: raise ValueError('unknown test-only fault boundary')
            ready.put({'hook':hook,'guard':execution.guard})
            await asyncio.Event().wait()
            return result
        executor.execute=at_boundary
        try: await Worker(backend,'crash-child',registry).run_once()
        finally: await backend.close()
    asyncio.run(run())

class ProcessFault:
    def __init__(self,url,hook,ledger):
        self.context=multiprocessing.get_context('spawn');self.ready=self.context.Queue()
        self.process=self.context.Process(target=crash_worker,args=(url,hook,self.ready,str(ledger)))

    async def crash(self):
        self.process.start()
        try:
            boundary=await asyncio.to_thread(self.ready.get,True,15)
            os.kill(self.process.pid,signal.SIGKILL)
            await asyncio.to_thread(self.process.join,5)
            assert self.process.exitcode==-signal.SIGKILL
            return boundary
        finally:
            if self.process.is_alive(): self.process.kill();self.process.join(5)
            self.ready.close();self.ready.join_thread()


def ledger_count(path,value):
    return Path(path).read_text().splitlines().count(value) if Path(path).exists() else 0


def graceful_worker(settings_data,ready,ledger):
    async def run():
        from workbench_agent.settings import Settings
        from workbench_agent.persistence.backends.sqlite import SQLiteBackend
        from workbench_agent.runtime.graphs.registry import foundation_worker_registry
        from workbench_agent.cli.worker import run_worker
        settings=Settings.model_validate(settings_data)
        backend=SQLiteBackend(settings.storage.database_url.get_secret_value());registry=foundation_worker_registry(backend)
        for executor in registry._executors.values():
            async def blocked(execution):
                ready.put(execution.guard)
                try: await asyncio.Event().wait()
                finally: _record(ledger,'cancelled_inflight')
            executor.execute=blocked
        try:
            result=await run_worker(settings,registry=registry)
            _record(ledger,result['status'])
        finally: await backend.close()
    asyncio.run(run())

"""Real persistence and incremental ASGI transport helpers for T08."""
import asyncio
from support.jobs import claim

class EventPlatform:
    def __init__(self,p):
        from workbench_agent.application.results import ResultService
        from workbench_agent.persistence.events import RunEventStore
        self.p=p; self.results=ResultService(p.backend); self.events=RunEventStore(p.backend)

    async def finalizable_run(self,*,complete=True):
        from workbench_agent.domain.outcomes import RunOutcome
        run=await self.p.create_run(); owned=await claim(self.p)
        outcome=RunOutcome(kind='foundation.recorded.v1',completed=complete,summary='Foundation recording only; no diagnosis performed.',
            findings=(),root_cause_hypotheses=(),impact=None,evidence_refs=(),
            unresolved_items=() if complete else ('Recording incomplete.',),proposed_actions=(),knowledge_gaps=(),usage_summary={})
        return run,owned.guard,outcome

    async def read_events(self,run_id,after_seq=0):
        return await self.events.read_batch(self.p.auth,run_id,after_seq,100)

    async def persisted_delta_texts(self,run_id):
        # Merger tests inspect durable writes; public event reads apply disclosure.
        import json
        from sqlalchemy import select
        from workbench_agent.persistence import schema
        async with self.p.backend.create_uow().begin_read() as uow:
            payloads = (await uow.execute(select(schema.run_events.c.payload).where(
                schema.run_events.c.team_id == self.p.auth.team_id,
                schema.run_events.c.run_id == run_id, schema.run_events.c.type == 'assistant.delta')
                .order_by(schema.run_events.c.seq))).scalars().all()
        return [json.loads(payload)['text'] for payload in payloads]

class StreamingClient:
    """Calls actual ASGI send incrementally; bounded queue imposes network backpressure."""
    def __init__(self,app,path,token,headers=()):
        self.app=app; self.path=path; self.token=token; self.headers=headers
        self.queue=asyncio.Queue(maxsize=1); self.disconnected=asyncio.Event()
        self.send_blocked=asyncio.Event()

    async def __aenter__(self):
        path,_,query=self.path.partition('?')
        scope={'type':'http','asgi':{'version':'3.0','spec_version':'2.4'},'http_version':'1.1',
            'method':'GET','scheme':'http','path':path,'raw_path':path.encode(),'query_string':query.encode(),
            'headers':[(b'authorization',self.token.encode()),*self.headers],
            'client':('127.0.0.1',1234),'server':('test',80),'root_path':''}
        async def receive():
            await self.disconnected.wait();return {'type':'http.disconnect'}
        async def send(message):
            if self.queue.full(): self.send_blocked.set()
            await self.queue.put(message)
        self.task=asyncio.create_task(self.app(scope,receive,send))
        self.start=await asyncio.wait_for(self.queue.get(),3)
        return self

    async def chunk(self):
        return await asyncio.wait_for(self.queue.get(),3)

    async def __aexit__(self,*args):
        self.disconnected.set();self.task.cancel()
        await asyncio.gather(self.task,return_exceptions=True)

def token(p): return f'Bearer development:{p.auth.team_id}:{p.auth.subject}'

async def save_outcome(p,guard,outcome):
    from langgraph.checkpoint.base import empty_checkpoint
    from workbench_agent.runtime.checkpoints.portable import PortableCheckpointSaver,InvocationGuard
    from workbench_agent.persistence.checkpoints.codec import CheckpointCodec
    provider=InvocationGuard();saver=PortableCheckpointSaver(p.backend,provider,CheckpointCodec())
    checkpoint=empty_checkpoint();checkpoint['channel_values']={'run_id':guard.run_id,'outcome':outcome}
    with provider.bind(guard):
        await saver.aput({'configurable':{'thread_id':guard.run_id,'checkpoint_ns':''}},checkpoint,{}, {})

async def evidence(p,run,*,quality='valid',region='ID'):
    from uuid import uuid4
    from sqlalchemy import insert
    from workbench_agent.persistence import schema as s
    async with p.backend.create_uow().begin_write() as uow:
        scope=await p.contexts.query_scope(uow,p.auth,run.run_id,{'region':region})
        evidence_id=str(uuid4())
        await uow.execute(insert(s.evidence).values(team_id=p.auth.team_id,run_id=run.run_id,evidence_id=evidence_id,
            scope_id=scope['scope_id'],source='test-observation',quality=quality,time_window='{}'))
    return evidence_id

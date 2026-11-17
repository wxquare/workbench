"""Single execution-event sequence allocator; T08 adds read/control delivery."""
import json
from typing import get_args
from workbench_agent.domain.events import EventType
from sqlalchemy import update, insert, select, func
from workbench_agent.persistence import schema as s
from workbench_agent.persistence.errors import StaleExecution
from workbench_agent.domain.guards import EXECUTING_RUN_STATUSES

async def execution_attempt(uow,team_id,run_id):
    """Actual durable execution claims; projection-only ownership is not an attempt."""
    return (await uow.execute(select(func.coalesce(func.sum(s.run_jobs.c.attempt),0)).where(
        s.run_jobs.c.team_id==team_id,s.run_jobs.c.run_id==run_id))).scalar_one()

class SQLiteExecutionEventStore:
    def __init__(self, clock, guard):
        self.clock, self.guard = clock, guard

    async def append_execution(self,uow,guard,event_type,payload,*,message_id=None):
        if event_type not in get_args(EventType):
            raise ValueError('canonical execution event required')
        await self.guard.require(uow,guard,EXECUTING_RUN_STATUSES)
        return await self.append_control(uow,guard.team_id,guard.run_id,event_type,payload,message_id=message_id)

    async def append_control(self,uow,team_id,run_id,event_type,payload,*,message_id=None):
        if event_type not in get_args(EventType): raise ValueError('canonical event required')
        body=json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
        now=await self.clock.now_ms(uow)
        result=await uow.execute(update(s.runs).where(s.runs.c.team_id==team_id,s.runs.c.run_id==run_id)
            .values(last_event_seq=s.runs.c.last_event_seq+1).returning(s.runs.c.last_event_seq))
        seq=result.scalar_one_or_none()
        if seq is None: raise StaleExecution()
        await uow.execute(insert(s.run_events).values(team_id=team_id,run_id=run_id,seq=seq,type=event_type,
            attempt=await execution_attempt(uow,team_id,run_id),payload=body,occurred_at=now,schema_version='1',message_id=message_id))
        return seq

    async def rows(self,uow,team_id,run_id,after_seq,limit):
        return list((await uow.execute(select(s.run_events).where(s.run_events.c.team_id==team_id,
            s.run_events.c.run_id==run_id,s.run_events.c.seq>after_seq).order_by(s.run_events.c.seq).limit(limit))).mappings())

    async def expired_floor(self,uow,team_id,run_id,cutoff):
        return (await uow.execute(select(func.max(s.run_events.c.seq)).where(s.run_events.c.team_id==team_id,
            s.run_events.c.run_id==run_id,s.run_events.c.occurred_at<=cutoff))).scalar_one() or 0

    async def prune(self,uow,team_id,run_id,cutoff):
        from sqlalchemy import delete
        floor=await self.expired_floor(uow,team_id,run_id,cutoff)
        if floor:
            await uow.execute(update(s.runs).where(s.runs.c.team_id==team_id,s.runs.c.run_id==run_id,s.runs.c.event_floor<floor).values(event_floor=floor))
            await uow.execute(delete(s.run_events).where(s.run_events.c.team_id==team_id,s.run_events.c.run_id==run_id,s.run_events.c.seq<=floor))

    async def require_delivery(self,uow,guard):
        now=await self.clock.now_ms(uow)
        row=(await uow.execute(select(s.outbox.c.outbox_id).join(s.actions,s.actions.c.action_id==s.outbox.c.action_id).where(
            s.outbox.c.team_id==guard.team_id,s.outbox.c.outbox_id==guard.outbox_id,s.outbox.c.action_id==guard.action_id,
            s.outbox.c.lease_owner==guard.worker_id,s.outbox.c.lease_until>now,s.outbox.c.revision==guard.revision,s.outbox.c.status=='leased',
            s.actions.c.team_id==guard.team_id,s.actions.c.run_id==guard.run_id))).first()
        if row is None: raise StaleExecution()

    async def supersede_drafts(self,uow,guard,*,final_message_id=None):
        attempt=await execution_attempt(uow,guard.team_id,guard.run_id)
        drafts=list((await uow.execute(select(s.run_events.c.message_id,s.run_events.c.attempt).where(
            s.run_events.c.team_id==guard.team_id,s.run_events.c.run_id==guard.run_id,s.run_events.c.type=='assistant.delta',
            s.run_events.c.attempt<(attempt+1 if final_message_id else attempt),s.run_events.c.message_id.is_not(None)).distinct())).all())
        for message_id,old_attempt in drafts:
            exists=(await uow.execute(select(s.run_events.c.seq).where(s.run_events.c.run_id==guard.run_id,
                s.run_events.c.type=='assistant.message_superseded',s.run_events.c.message_id==message_id).limit(1))).first()
            if exists is None:
                await self.append_execution(uow,guard,'assistant.message_superseded',
                    {'message_id':message_id,'attempt':old_attempt,'superseded':True,'replaced_by_attempt':attempt,**({'replaced_by_message_id':final_message_id} if final_message_id else {})},message_id=message_id)

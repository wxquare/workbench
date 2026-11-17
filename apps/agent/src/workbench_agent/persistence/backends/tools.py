"""Tool invocations and evidence persisted through caller UoW."""
from sqlalchemy import select, insert, update, func
from workbench_agent.persistence import schema

class SQLiteToolStore:
    async def next_version(self,uow,team,agent,alias):
        t=schema.tool_bindings
        value=(await uow.execute(select(func.max(t.c.version)).where(t.c.team_id==team,t.c.agent_id==agent,t.c.alias==alias))).scalar_one()
        return (value or 0)+1
    async def invocation(self,uow,team,run,logical):
        t=schema.tool_invocations
        return (await uow.execute(select(t).where(t.c.team_id==team,t.c.run_id==run,t.c.logical_call_id==logical).order_by(t.c.attempt.desc()).limit(1))).mappings().first()
    async def logical_invocation_count(self,uow,team,run):
        t=schema.tool_invocations
        return (await uow.execute(select(func.count(func.distinct(t.c.logical_call_id))).where(
            t.c.team_id==team,t.c.run_id==run))).scalar_one()
    async def invocation_by_id(self,uow,team,run,invocation):
        t=schema.tool_invocations
        return (await uow.execute(select(t).where(t.c.team_id==team,t.c.run_id==run,t.c.invocation_id==invocation))).mappings().first()
    async def succeeded_invocations(self,uow,team,run):
        t=schema.tool_invocations
        return (await uow.execute(select(t).where(t.c.team_id==team,t.c.run_id==run,t.c.status=='succeeded',t.c.receipt_ref.is_not(None)))).mappings().all()
    async def insert_invocation(self,uow,values):
        await uow.execute(insert(schema.tool_invocations).values(**values))
    async def complete_invocation(self,uow,team,run,invocation,status,receipt,now):
        t=schema.tool_invocations
        result=await uow.execute(update(t).where(t.c.team_id==team,t.c.run_id==run,t.c.invocation_id==invocation,
            t.c.status=='possibly_sent',t.c.receipt_ref.is_(None)).values(status=status,receipt_ref=receipt,updated_at=now))
        return result.rowcount==1
    async def insert_evidence(self,uow,values):
        await uow.execute(insert(schema.evidence).values(**values))

    async def insert_evidence_payload(self,uow,values):
        await uow.execute(insert(schema.evidence_payloads).values(**values))
    async def evidence_payload(self,uow,team,run,evidence):
        t=schema.evidence_payloads
        return (await uow.execute(select(t).where(t.c.team_id==team,t.c.run_id==run,t.c.evidence_id==evidence))).mappings().first()

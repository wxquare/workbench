"""SQLite atomic admission and scoped reads, using the caller's transaction."""
from sqlalchemy import select, insert, update, and_, or_
from workbench_agent.persistence import schema

TABLES = {name: getattr(schema, name) for name in ('threads', 'runs', 'messages', 'run_jobs', 'run_events', 'source_events', 'interrupts', 'resume_requests', 'query_scope_revisions')}

class SQLiteRunStore:
    async def one(self, uow, resource, **scope):
        table = TABLES[resource]
        return (await uow.execute(select(table).where(*(table.c[k] == v for k, v in scope.items())))).mappings().first()

    async def insert(self, uow, resource, values):
        await uow.execute(insert(TABLES[resource]).values(**values))

    async def replace_source(self, uow, source_event_id, values):
        await uow.execute(update(schema.source_events).where(schema.source_events.c.source_event_id == source_event_id).values(**values))

    async def messages(self, uow, team_id, thread_id, *, limit=20, after=None):
        table = schema.messages
        statement = select(table).where(table.c.team_id == team_id, table.c.thread_id == thread_id)
        if after is not None:
            cursor = await self.one(uow, 'messages', team_id=team_id, thread_id=thread_id, message_id=after)
            if cursor is None:
                return []
            statement = statement.where(or_(table.c.created_at > cursor['created_at'],
                and_(table.c.created_at == cursor['created_at'], table.c.message_id > after)))
        return list((await uow.execute(statement.order_by(table.c.created_at, table.c.message_id).limit(limit))).mappings())

    async def change(self,uow,resource,scope,values):
        table=TABLES[resource]
        result=await uow.execute(update(table).where(*(table.c[k]==v for k,v in scope.items())).values(**values))
        return result.rowcount

    async def generation(self,uow,run_id):
        from sqlalchemy import func
        return (await uow.execute(select(func.max(schema.run_jobs.c.generation)).where(schema.run_jobs.c.run_id==run_id))).scalar_one() or 0

    async def current_scope(self,uow,team_id,run_id):
        t=schema.query_scope_revisions
        return (await uow.execute(select(t).where(t.c.team_id==team_id,t.c.run_id==run_id).order_by(t.c.version.desc()).limit(1))).mappings().first()

    async def waiting(self,uow,team_id,run_id):
        t=schema.interrupts
        return (await uow.execute(select(t).where(t.c.team_id==team_id,t.c.run_id==run_id,t.c.status=='open').order_by(t.c.created_at.desc()).limit(1))).mappings().first()

    async def resume_pending(self,uow,team_id,run_id):
        i,r=schema.interrupts,schema.resume_requests
        return (await uow.execute(select(r,i.c.graph_interrupt_id,i.c.schema,i.c.args_hash).join(i,i.c.accepted_resume_request_id==r.c.request_id).where(
            r.c.team_id==team_id,r.c.run_id==run_id,r.c.status.in_(['submitted','applying'])))).mappings().first()

    async def checkpoint(self,uow,team_id,run_id):
        from workbench_agent.persistence.checkpoints.codec import CheckpointCodec
        t=schema.graph_checkpoints
        row=(await uow.execute(select(t).where(t.c.team_id==team_id,t.c.run_id==run_id,t.c.namespace=='').order_by(t.c.checkpoint_id.desc()).limit(1))).mappings().first()
        if row is None: return None
        codec=CheckpointCodec(); values=codec.loads_typed((row['payload_type'],row['payload']))['channel_values']
        w=schema.graph_pending_writes
        pending=(await uow.execute(select(w).where(w.c.team_id==team_id,w.c.run_id==run_id,w.c.namespace_hash==row['namespace_hash'],w.c.checkpoint_id==row['checkpoint_id'],w.c.channel=='__interrupt__'))).mappings().all()
        interrupts=[]
        for item in pending:
            interrupts.extend(codec.loads_typed((item['payload_type'],item['payload'])))
        return {'checkpoint_id':row['checkpoint_id'],'values':values,'interrupts':interrupts}

    async def active_until(self,uow,team_id,run_id):
        t=schema.execution_leases
        return (await uow.execute(select(t.c.expires_at).where(t.c.team_id==team_id,t.c.owner_run_id==run_id))).scalar_one_or_none()

    async def all_evidence(self,uow,team_id,run_id):
        t=schema.evidence
        return list((await uow.execute(select(t).where(t.c.team_id==team_id,t.c.run_id==run_id))).mappings())

    async def current_evidence(self,uow,team_id,run_id):
        scope=await self.current_scope(uow,team_id,run_id)
        if scope is None: return []
        t=schema.evidence
        return list((await uow.execute(select(t).where(t.c.team_id==team_id,t.c.run_id==run_id,
            t.c.scope_id==scope['scope_id']))).mappings())

    async def has_actions(self,uow,team_id,run_id):
        return (await uow.execute(select(schema.actions.c.action_id).where(schema.actions.c.team_id==team_id,schema.actions.c.run_id==run_id).limit(1))).first() is not None

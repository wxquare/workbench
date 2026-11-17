"""SQLite action/approval/outbox operations; all use the caller's UoW."""
from sqlalchemy import insert, select, update, and_, or_, case, func
from workbench_agent.persistence import schema as s


class SQLiteActionStore:
    async def one(self, uow, table, **scope):
        t = getattr(s, table)
        return (await uow.execute(select(t).where(*(t.c[k] == v for k, v in scope.items())))).mappings().first()

    async def insert(self, uow, table, values):
        await uow.execute(insert(getattr(s, table)).values(**values))

    async def change(self, uow, table, scope, values):
        t = getattr(s, table)
        result = await uow.execute(update(t).where(*(t.c[k] == v for k, v in scope.items())).values(**values))
        return result.rowcount == 1

    async def current_approval(self,uow,team_id,action_id):
        return (await uow.execute(select(s.approvals).where(s.approvals.c.team_id==team_id,
            s.approvals.c.action_id==action_id).order_by(s.approvals.c.created_at.desc(),s.approvals.c.approval_id.desc()).limit(1))).mappings().first()

    async def candidate(self, uow, now, destinations):
        if not destinations: return None
        # Exclude unfinished synthetic runs in SQL so any number of blocked
        # rows cannot starve another destination, Run or tenant.
        graph=case((func.json_valid(s.runs.c.versions)==1,func.json_extract(s.runs.c.versions,'$.graph_version')),else_='')
        from workbench_agent.domain.guards import TERMINAL_RUN_STATUSES
        return (await uow.execute(select(s.outbox).join(s.actions,and_(s.actions.c.team_id==s.outbox.c.team_id,
            s.actions.c.action_id==s.outbox.c.action_id)).join(s.runs,and_(s.runs.c.team_id==s.actions.c.team_id,
            s.runs.c.run_id==s.actions.c.run_id)).where(s.outbox.c.status=='pending',
            s.outbox.c.available_at<=now,s.outbox.c.destination.in_(destinations),
            or_(graph.is_(None),graph.not_in(('dod.v14','dod.v15')),s.runs.c.status.in_(TERMINAL_RUN_STATUSES)))
            .order_by(s.outbox.c.created_at,s.outbox.c.outbox_id).limit(1))).mappings().first()

    async def expired(self, uow, now):
        return list((await uow.execute(select(s.outbox).where(s.outbox.c.status.in_(('leased','sending')),
            s.outbox.c.lease_until<=now).limit(100))).mappings())

    async def policy(self,uow):
        return (await uow.execute(select(s.action_policy_state).where(s.action_policy_state.c.policy_id=='deployment'))).mappings().first()

    async def insert_policy(self,uow,values):
        await uow.execute(insert(s.action_policy_state).values(**values))

    async def update_policy(self,uow,revision,values):
        result=await uow.execute(update(s.action_policy_state).where(s.action_policy_state.c.policy_id=='deployment',
            s.action_policy_state.c.revision==revision).values(**values))
        return result.rowcount==1

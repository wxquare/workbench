"""Atomic control transitions. No graph/network calls inside these transactions."""
from sqlalchemy import select,update,delete,func
from workbench_agent.persistence import schema as s
from workbench_agent.domain.guards import TERMINAL_RUN_STATUSES
from workbench_agent.domain.transitions import active_elapsed
from workbench_agent.application.errors import DomainError

class SQLiteControlStore:
    def __init__(self,backend): self.backend=backend

    async def status(self,uow,team_id,run_id):
        return (await uow.execute(select(s.runs.c.status).where(s.runs.c.team_id==team_id,s.runs.c.run_id==run_id))).scalar_one_or_none()

    async def request_cancel(self,uow,run):
        if run['status']=='finalizing' or run['recovery_phase']=='finalizing': raise DomainError('FINALIZATION_STARTED',409)
        if run['status'] in TERMINAL_RUN_STATUSES or run['status']=='cancel_requested': return
        now=await self.backend.clock.now_ms(uow)
        if run['status']=='running':
            await uow.execute(update(s.runs).where(s.runs.c.run_id==run['run_id']).values(status='cancel_requested',revision=s.runs.c.revision+1,updated_at=now))
            await self.backend.execution_events.append_control(uow,run['team_id'],run['run_id'],'run.status_changed',
                {'status':'cancel_requested','revision':run['revision']+1,'reason':'cancel_requested'})
        else: await self.finish(uow,run,'cancelled')

    async def finish_cancel(self,uow,guard):
        # Dedicated control fence, never broaden normal Saver/operation authority.
        await self.backend.jobs.lock_scope(uow,guard)
        await self.backend.lease_guard.require(uow,guard,frozenset({'cancel_requested'}))
        run=await self.backend.runs.one(uow,'runs',team_id=guard.team_id,run_id=guard.run_id)
        await self.finish(uow,run,'cancelled')

    async def due(self,uow):
        from sqlalchemy import or_,and_
        now=await self.backend.clock.now_ms(uow)
        return list((await uow.execute(select(s.runs).where(s.runs.c.status.not_in(TERMINAL_RUN_STATUSES),or_(
            s.runs.c.wall_deadline_at<=now,
            and_(s.runs.c.status.in_(['waiting_input','waiting_approval']),s.runs.c.waiting_deadline_at<=now),
            and_(s.runs.c.status.in_(['running','finalizing','cancel_requested']),s.runs.c.deadline_at<=now))))).mappings())

    async def finish(self,uow,run,status):
        """Explicit authoritative control timeout/cancel, fenced by locked Run row.

        SQLite's BEGIN IMMEDIATE serializes competing cancel/resume/project writes.
        Row-lock adapters must acquire quota/Thread/Lease/Run in the same order.
        """
        now=await self.backend.clock.now_ms(uow)
        lease=(await uow.execute(select(s.execution_leases).where(s.execution_leases.c.owner_run_id==run['run_id']))).mappings().first()
        elapsed=active_elapsed(run,now,lease['expires_at'] if lease else None)
        terminal_event='run.failed' if status in ('failed','timed_out') else 'run.'+status
        await self.backend.execution_events.append_control(uow,run['team_id'],run['run_id'],terminal_event,{'terminal_status':status,'links':{'result':f"/v1/runs/{run['run_id']}"}})
        await self.backend.execution_events.append_control(uow,run['team_id'],run['run_id'],'run.status_changed',{'status':status,'revision':run['revision']+1})
        await uow.execute(update(s.runs).where(s.runs.c.run_id==run['run_id'],s.runs.c.revision==run['revision']).values(
            status=status,revision=run['revision']+1,recovery_phase=None,active_elapsed_ms=elapsed,active_since=None,
            deadline_at=None,waiting_deadline_at=None,updated_at=now))
        await self._settle_resumes(uow,run,status,now)
        await uow.execute(update(s.run_jobs).where(s.run_jobs.c.run_id==run['run_id'],s.run_jobs.c.status.not_in(['done','dead'])).values(
            status='done' if status=='cancelled' else 'dead',lease_owner=None,lease_until=None,revision=s.run_jobs.c.revision+1,updated_at=now))
        await uow.execute(delete(s.execution_leases).where(s.execution_leases.c.owner_run_id==run['run_id']))
        await uow.execute(update(s.threads).where(s.threads.c.thread_id==run['thread_id'],s.threads.c.active_run_id==run['run_id']).values(
            active_run_id=None,revision=s.threads.c.revision+1,updated_at=now))
        live=(await uow.execute(select(func.count()).select_from(s.execution_leases).where(s.execution_leases.c.team_id==run['team_id'],s.execution_leases.c.expires_at>now))).scalar_one()
        await uow.execute(update(s.teams).where(s.teams.c.team_id==run['team_id']).values(active_executions=live,revision=s.teams.c.revision+1,updated_at=now))

    async def _settle_resumes(self,uow,run,status,now):
        """Keep selected request history truthful at the terminal linearization point.

        An applying request is not presumed unapplied: the durable checkpoint must
        prove either its marker or the exact still-pending named interrupt. The
        same write transaction fences any racing Saver/Command before cleanup.
        """
        from workbench_agent.application.resume import ResumeApplier
        i,r=s.interrupts,s.resume_requests
        selected=(await uow.execute(select(r,i.c.graph_interrupt_id,i.c.schema,i.c.args_hash)
            .join(i,(i.c.accepted_resume_request_id==r.c.request_id)&(i.c.interrupt_id==r.c.interrupt_id))
            .where(r.c.team_id==run['team_id'],r.c.run_id==run['run_id'],
                i.c.team_id==run['team_id'],i.c.run_id==run['run_id']))).mappings().all()
        saved=None
        for request in selected:
            if request['status'] in ('submitted','applying'):
                if saved is None: saved=await self.backend.runs.checkpoint(uow,run['team_id'],run['run_id'])
                marked=bool(saved and saved['values'].get('last_applied_resume_id')==request['request_id'])
                if request['status']=='applying' and not marked:
                    if saved is None: raise ValueError('applying resume lacks durable evidence')
                    ResumeApplier().apply(request,saved)  # Exact pending interrupt proves no application.
                await uow.execute(update(r).where(r.c.request_id==request['request_id']).values(
                    status='applied' if marked else 'rejected',updated_at=now))
            else: marked=request['status']=='applied'
            if marked:
                await uow.execute(update(i).where(i.c.interrupt_id==request['interrupt_id'],
                    i.c.accepted_resume_request_id==request['request_id']).values(status='consumed',updated_at=now))
        await uow.execute(update(i).where(i.c.team_id==run['team_id'],i.c.run_id==run['run_id'],
            i.c.status.in_(['open','response_submitted'])).values(
                status='expired' if status in ('expired','timed_out') else 'cancelled',updated_at=now))

"""SQLite claims/lifecycle. Every mutation uses the caller's short write UoW."""
import json
from sqlalchemy import select, update, insert, delete, and_, or_, func, case
from workbench_agent.domain.guards import (ExecutionGuard, ClaimedJob, RunReleaseStatus, JobQueueStatus,
    WAITING_RUN_STATUSES, TERMINAL_RUN_STATUSES, EXECUTING_RUN_STATUSES, PROJECTION_RECOVERY_PHASES)
from workbench_agent.domain.context import RunContext
from workbench_agent.persistence import schema as s
from workbench_agent.persistence.errors import StaleExecution
from workbench_agent.persistence.backends.execution_events import execution_attempt
from workbench_agent.domain.transitions import active_elapsed

class SQLiteJobStore:
    def __init__(self,clock,guard,events):
        self.clock,self.guard,self.events=clock,guard,events

    async def execution_attempt(self,uow,team_id,run_id):
        return await execution_attempt(uow,team_id,run_id)

    async def candidates(self,uow,limit=64,*,after=None,run_id=None):
        now = await self.clock.now_ms(uow)
        j=s.run_jobs
        level=case((j.c.kind.in_(['resume','recover']),0),(j.c.kind=='alert',2),(j.c.kind=='schedule',3),else_=1)
        due=or_(j.c.status.in_(['ready','retry_wait','capability_blocked']),
            and_(j.c.status=='leased',j.c.lease_until<=now))
        ranked=select(j,level.label('_level'),func.row_number().over(
            partition_by=(j.c.team_id,level),order_by=(j.c.created_at,j.c.job_id)).label('_team_rank')).where(
                j.c.available_at<=now,due).subquery()
        order=(ranked.c._level,ranked.c._team_rank,ranked.c.created_at,ranked.c.job_id)
        statement=select(ranked)
        if run_id is not None: statement=statement.where(ranked.c.run_id==run_id)
        if after is not None:
            statement=statement.where(or_(*(and_(*(order[k]==after[k] for k in range(index)),
                order[index]>after[index]) for index in range(len(order)))))
        return list((await uow.execute(statement.order_by(*order).limit(min(limit,64)))).mappings())

    async def _segment_attempt(self,uow,job):
        # Recover generations share a durable segment; human resume starts one.
        anchor=(await uow.execute(select(func.max(s.run_jobs.c.generation)).where(
            s.run_jobs.c.run_id==job['run_id'],s.run_jobs.c.kind=='resume',
            s.run_jobs.c.generation<=job['generation']))).scalar_one() or 1
        return (await uow.execute(select(func.coalesce(func.sum(s.run_jobs.c.attempt),0)).where(
            s.run_jobs.c.run_id==job['run_id'],s.run_jobs.c.generation>=anchor))).scalar_one()

    @staticmethod
    def _projection_only(run,attempts):
        # This Run marker survives Job leased/ready transitions, crashes and
        # unsupported-worker scans. A scheduling status is never recovery intent.
        return run['recovery_phase'] in PROJECTION_RECOVERY_PHASES or run['status']=='finalizing' or attempts>=3

    @staticmethod
    def _supports(capabilities,context,job,projection_only):
        versions=capabilities.projection_graph_ids if projection_only else capabilities.graph_ids
        return (not projection_only or capabilities.can_reconcile) and context.graph_id in versions and set(
            json.loads(job['capabilities'])).issubset(capabilities.features)

    async def has_priority_work(self,uow,capabilities):
        """Bounded admin admission check; never consumes a Run claim or epoch."""
        now=await self.clock.now_ms(uow)
        total=(await uow.execute(select(func.count()).select_from(s.execution_leases).where(
            s.execution_leases.c.expires_at>now))).scalar_one()
        if total>=2:
            return False
        for job in await self.candidates(uow):
            run=(await uow.execute(select(s.runs).where(s.runs.c.run_id==job['run_id']))).mappings().one()
            if run['status'] not in {'queued','running','finalizing','cancel_requested','waiting_input','waiting_approval'}:
                continue
            if run['status'] in WAITING_RUN_STATUSES and job['kind']!='resume':
                continue
            context=RunContext.model_validate_json(run['versions'])
            projection_only=self._projection_only(run,await self._segment_attempt(uow,job))
            if not self._supports(capabilities,context,job,projection_only):
                continue
            thread=(await uow.execute(select(s.threads).where(s.threads.c.thread_id==run['thread_id']))).mappings().one()
            if thread['active_run_id'] not in {None,run['run_id']}:
                continue
            lease=(await uow.execute(select(s.execution_leases).where(s.execution_leases.c.thread_id==run['thread_id']))).mappings().first()
            if lease and lease['expires_at']>now:
                continue
            team=(await uow.execute(select(s.teams).where(s.teams.c.team_id==job['team_id']))).mappings().one()
            live=(await uow.execute(select(func.count()).select_from(s.execution_leases).where(
                s.execution_leases.c.team_id==job['team_id'],s.execution_leases.c.expires_at>now))).scalar_one()
            if team['status']=='active' and live<team['execution_limit']:
                return True
        return False

    async def claim(self,uow,capabilities,worker_id,candidates,*,projection=False):
        now = await self.clock.now_ms(uow)
        for candidate in candidates:
            claim = await self._candidate(uow,candidate,capabilities,worker_id,now,projection=projection)
            if claim:
                return claim,candidate
        return None,None

    async def _candidate(self,uow,j,capabilities,worker_id,now,*,projection=False):
        # Re-read in the owning write transaction; no stale read can become ownership.
        job=(await uow.execute(select(s.run_jobs).where(s.run_jobs.c.job_id==j['job_id']))).mappings().one()
        if job['revision']!=j['revision'] or job['available_at']>now:
            return None
        if job['status'] not in {'ready','retry_wait','capability_blocked','leased'}:
            return None
        if job['status']=='leased' and (job['lease_until'] is None or job['lease_until']>now):
            return None
        run=(await uow.execute(select(s.runs).where(s.runs.c.run_id==job['run_id']))).mappings().one()
        if run['status'] not in {'queued','running','finalizing','cancel_requested','waiting_input','waiting_approval'}:
            return None
        context=RunContext.model_validate_json(run['versions'])
        attempts=await self._segment_attempt(uow,job)
        projection_only=projection or job['status']=='leased' or run['status']=='cancel_requested' or self._projection_only(run,attempts)
        reason=(run['error_code'] or ('RETRY_EXHAUSTED' if attempts>=3 else 'PROJECTION_REQUIRED')) if projection_only else None
        if not self._supports(capabilities,context,job,projection_only):
            if job['status']!='leased':
                await uow.execute(update(s.run_jobs).where(s.run_jobs.c.job_id==job['job_id']).values(status='capability_blocked'))
            return None
        if run['status'] in WAITING_RUN_STATUSES and job['kind']!='resume':
            return None
        # Fixed lock/write order: quota -> Thread -> Lease -> Run -> Job.
        team=(await uow.execute(select(s.teams).where(s.teams.c.team_id==job['team_id']))).mappings().one()
        live=(await uow.execute(select(func.count()).select_from(s.execution_leases).where(
            s.execution_leases.c.team_id==team['team_id'],s.execution_leases.c.expires_at>now))).scalar_one()
        total=(await uow.execute(select(func.count()).select_from(s.execution_leases).where(s.execution_leases.c.expires_at>now))).scalar_one()
        thread=(await uow.execute(select(s.threads).where(s.threads.c.thread_id==run['thread_id']))).mappings().one()
        if thread['active_run_id'] not in {None,run['run_id']}:
            await uow.execute(update(s.run_jobs).where(s.run_jobs.c.job_id==job['job_id']).values(available_at=now+1000))
            return None
        lease=(await uow.execute(select(s.execution_leases).where(s.execution_leases.c.thread_id==run['thread_id']))).mappings().first()
        if lease and lease['expires_at']>now:
            return None
        if live>=team['execution_limit'] or total>=2 or team['status']!='active':
            return None
        elapsed=active_elapsed(run,now,lease['expires_at'] if lease else None)
        epoch=run['run_epoch']+1
        await uow.execute(update(s.teams).where(s.teams.c.team_id==team['team_id']).values(
            active_executions=live+1,revision=s.teams.c.revision+1,updated_at=now))
        await uow.execute(update(s.threads).where(s.threads.c.thread_id==run['thread_id']).values(
            active_run_id=run['run_id'],revision=s.threads.c.revision+1,updated_at=now))
        values=dict(team_id=team['team_id'],thread_id=run['thread_id'],owner_run_id=run['run_id'],owner_job_id=job['job_id'],
            worker_id=worker_id,run_epoch=epoch,expires_at=now+45000,guard_revision=0)
        if lease:
            await uow.execute(update(s.execution_leases).where(s.execution_leases.c.thread_id==run['thread_id']).values(**values))
        else:
            await uow.execute(insert(s.execution_leases).values(**values))
        recovery_phase=(run['recovery_phase'] if run['recovery_phase'] in PROJECTION_RECOVERY_PHASES else
            'finalizing' if run['status']=='finalizing' else 'projection') if projection_only else run['recovery_phase']
        await uow.execute(update(s.runs).where(s.runs.c.run_id==run['run_id']).values(
            status=run['status'] if run['status'] in ('finalizing','cancel_requested') else 'running',run_epoch=epoch,recovery_phase=recovery_phase,
            revision=s.runs.c.revision+1,started_at=run['started_at'] if run['started_at'] is not None else now,
            active_elapsed_ms=elapsed,active_since=now,deadline_at=min(run['wall_deadline_at'],now+max(0,json.loads(run['budgets'])['wall_seconds']*1000-elapsed)),updated_at=now))
        await uow.execute(update(s.run_jobs).where(s.run_jobs.c.job_id==job['job_id']).values(status='leased',
            lease_owner=worker_id,lease_until=now+45000,attempt=job['attempt']+(0 if projection_only else 1),revision=s.run_jobs.c.revision+1,updated_at=now))
        guard=ExecutionGuard(team['team_id'],run['thread_id'],run['run_id'],job['job_id'],worker_id,epoch)
        if not projection_only:
            await self.events.supersede_drafts(uow,guard)
            await self.events.append_execution(uow,guard,'run.started',{'graph_id':context.graph_id,'generation':job['generation']})
        return ClaimedJob(guard,context,job['kind'],job['generation'],job['attempt']+(0 if projection_only else 1),now+45000,attempts+(0 if projection_only else 1),projection_only,reason)

    async def renew(self,uow,guard):
        await self.guard.require(uow,guard,EXECUTING_RUN_STATUSES)
        now=await self.clock.now_ms(uow)
        await uow.execute(update(s.execution_leases).where(s.execution_leases.c.thread_id==guard.thread_id).values(expires_at=now+45000))
        await uow.execute(update(s.run_jobs).where(s.run_jobs.c.job_id==guard.job_id).values(lease_until=now+45000))

    async def lock_scope(self,uow,guard):
        # Acquire earlier scope rows before LeaseGuard when projection may change
        # quota/Thread. SQLite serializes writes globally; row-lock adapters must
        # preserve this order too. These no-op writes do not change revisions.
        await uow.execute(update(s.teams).where(s.teams.c.team_id==guard.team_id)
            .values(active_executions=s.teams.c.active_executions))
        await uow.execute(update(s.threads).where(s.threads.c.team_id==guard.team_id,
            s.threads.c.thread_id==guard.thread_id).values(active_run_id=s.threads.c.active_run_id))

    async def release(self,uow,guard,*,status:RunReleaseStatus,job_status:JobQueueStatus | None=None,
                      recovery_phase:str | None=None,retry_delay_ms=0,error_code=None):
        if status not in {'queued'} | WAITING_RUN_STATUSES | TERMINAL_RUN_STATUSES:
            raise ValueError('unsupported Run lifecycle status')
        if job_status is not None and (status!='queued' or job_status not in {'ready','retry_wait','capability_blocked'}):
            raise ValueError('queued Job scheduling status required')
        if retry_delay_ms<0 or (status!='queued' and (retry_delay_ms or recovery_phase is not None)):
            raise ValueError('recovery scheduling metadata requires queued Run')
        await self.lock_scope(uow,guard)
        await self.guard.require(uow,guard,EXECUTING_RUN_STATUSES)
        now=await self.clock.now_ms(uow)
        run=(await uow.execute(select(s.runs).where(s.runs.c.run_id==guard.run_id))).mappings().one()
        job=(await uow.execute(select(s.run_jobs).where(s.run_jobs.c.job_id==guard.job_id))).mappings().one()
        terminal=status in TERMINAL_RUN_STATUSES
        if status=='queued':
            job_status=job_status or 'retry_wait'
            # Queuing/capability rejection cannot erase projection-only intent.
            recovery_phase=('finalizing' if run['status']=='finalizing' else
                run['recovery_phase'] if run['recovery_phase'] in PROJECTION_RECOVERY_PHASES else
                recovery_phase or run['recovery_phase'] or (run['phase'] or 'running' if job_status=='retry_wait' else None))
        else:
            job_status='dead' if status in {'failed','timed_out'} else 'done'
            # Only the guarded waiting/terminal projector declares progress safely
            # persisted. T07/T09 own validation of its real Outcome/interrupt.
            recovery_phase=None
        if status=='queued' and recovery_phase in PROJECTION_RECOVERY_PHASES and error_code is None:
            error_code=run['error_code']
        revision=run['revision']+1
        recovering=status=='queued' and job_status=='retry_wait'
        event_type='run.recovery_scheduled' if recovering else 'run.status_changed'
        payload={'status':status,'revision':revision,'reason':error_code or job_status,
            'recovery_phase':recovery_phase}
        if recovering:
            payload.update(generation=job['generation'],attempt=await execution_attempt(uow,guard.team_id,guard.run_id))
        if status in {'failed','timed_out','cancelled','expired'}:
            terminal_event='run.failed' if status in {'failed','timed_out'} else 'run.'+status
            await self.events.append_execution(uow,guard,terminal_event,{'terminal_status':status,'reason':error_code,'links':{'result':f'/v1/runs/{guard.run_id}'}})
        await self.events.append_execution(uow,guard,event_type,payload)
        live=(await uow.execute(select(func.count()).select_from(s.execution_leases).where(
            s.execution_leases.c.team_id==guard.team_id,s.execution_leases.c.expires_at>now,
            s.execution_leases.c.thread_id!=guard.thread_id))).scalar_one()
        await uow.execute(update(s.teams).where(s.teams.c.team_id==guard.team_id).values(active_executions=live,
            revision=s.teams.c.revision+1,updated_at=now))
        if terminal:
            await uow.execute(update(s.threads).where(s.threads.c.thread_id==guard.thread_id,
                s.threads.c.active_run_id==guard.run_id).values(active_run_id=None,revision=s.threads.c.revision+1,updated_at=now))
        await uow.execute(delete(s.execution_leases).where(s.execution_leases.c.thread_id==guard.thread_id))
        await uow.execute(update(s.runs).where(s.runs.c.run_id==guard.run_id,s.runs.c.run_epoch==guard.run_epoch,
            s.runs.c.revision==run['revision']).values(status=status,recovery_phase=recovery_phase,
                error_code=error_code,revision=revision,active_elapsed_ms=active_elapsed(run,now),active_since=None,deadline_at=None,updated_at=now))
        await uow.execute(update(s.run_jobs).where(s.run_jobs.c.job_id==guard.job_id).values(
            status=job_status,lease_owner=None,lease_until=None,available_at=now+retry_delay_ms,
            revision=s.run_jobs.c.revision+1,updated_at=now))

    async def continue_recovery(self,uow,guard,checkpoint_id):
        """Only the reconciler's exact checkpoint decision clears projection intent.

        Close old generation and create one ready recover job in this same UoW.
        No graph, Command, model or external action runs here.
        """
        await self.guard.require(uow,guard,EXECUTING_RUN_STATUSES)
        saved=await self._checkpoint(uow,guard)
        if (saved['checkpoint_id'] if saved else None)!=checkpoint_id: raise StaleExecution()
        job=(await uow.execute(select(s.run_jobs).where(s.run_jobs.c.job_id==guard.job_id))).mappings().one()
        if await self._segment_attempt(uow,job)>=3: raise ValueError('retry budget exhausted')
        await self.release(uow,guard,status='queued',job_status='ready',recovery_phase='projection')
        now=await self.clock.now_ms(uow)
        await uow.execute(update(s.run_jobs).where(s.run_jobs.c.job_id==guard.job_id).values(status='done'))
        await uow.execute(update(s.runs).where(s.runs.c.run_id==guard.run_id,s.runs.c.run_epoch==guard.run_epoch).values(recovery_phase=None,error_code=None))
        from uuid import uuid4
        generation=(await uow.execute(select(func.max(s.run_jobs.c.generation)).where(s.run_jobs.c.run_id==guard.run_id))).scalar_one()+1
        await uow.execute(insert(s.run_jobs).values(team_id=guard.team_id,run_id=guard.run_id,job_id=str(uuid4()),generation=generation,
            kind='recover',status='ready',available_at=now,capabilities=job['capabilities'],created_at=now,updated_at=now))
        await self.events.append_control(uow,guard.team_id,guard.run_id,'run.recovery_scheduled',{'generation':generation,'checkpoint_id':checkpoint_id,'reason':'validated_foundation_continuation'})

    async def _checkpoint(self,uow,guard):
        from workbench_agent.persistence.backends.runs import SQLiteRunStore
        return await SQLiteRunStore().checkpoint(uow,guard.team_id,guard.run_id)

"""Strict transaction-local fencing; consumers write through this same UoW."""
from sqlalchemy import update, select, exists
from workbench_agent.domain.guards import ExecutionGuard, EXECUTING_RUN_STATUSES
from workbench_agent.persistence import schema as s
from workbench_agent.persistence.errors import StaleExecution

class LeaseGuard:
    def __init__(self, clock):
        self.clock = clock

    async def require(self, uow, guard: ExecutionGuard, allowed_statuses=EXECUTING_RUN_STATUSES) -> None:
        if not isinstance(guard, ExecutionGuard):
            raise TypeError('LeaseGuard requires ExecutionGuard')
        now = await self.clock.now_ms(uow)
        lease, run, job, thread = s.execution_leases, s.runs, s.run_jobs, s.threads
        owner = (lease.c.team_id==guard.team_id,lease.c.thread_id==guard.thread_id,
            lease.c.owner_run_id==guard.run_id,lease.c.owner_job_id==guard.job_id,
            lease.c.worker_id==guard.worker_id,lease.c.run_epoch==guard.run_epoch,lease.c.expires_at>now)
        current = exists(select(run.c.run_id).where(run.c.team_id==guard.team_id,run.c.run_id==guard.run_id,
            run.c.thread_id==guard.thread_id,run.c.run_epoch==guard.run_epoch,run.c.status.in_(allowed_statuses)))
        owned_job = exists(select(job.c.job_id).where(job.c.team_id==guard.team_id,job.c.job_id==guard.job_id,
            job.c.run_id==guard.run_id,job.c.status=='leased',job.c.lease_owner==guard.worker_id,job.c.lease_until>now))
        active = exists(select(thread.c.thread_id).where(thread.c.team_id==guard.team_id,
            thread.c.thread_id==guard.thread_id,thread.c.active_run_id==guard.run_id))
        changed = await uow.execute(update(lease).where(*owner,current,owned_job,active)
            .values(guard_revision=lease.c.guard_revision+1))
        if changed.rowcount != 1:
            await uow.rollback()
            raise StaleExecution()

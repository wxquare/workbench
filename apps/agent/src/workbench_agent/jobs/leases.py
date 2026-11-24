"""Lease operations; stale ownership never enters automatic retry."""

from workbench_agent.persistence.errors import StaleExecution
from workbench_agent.domain.guards import RunReleaseStatus, JobQueueStatus


class LeaseService:
    def __init__(self, backend):
        self.backend = backend

    async def renew(self, guard) -> bool:
        try:
            async with self.backend.create_uow().begin_write() as uow:
                await self.backend.jobs.renew(uow, guard)
            return True
        except StaleExecution:
            return False

    async def release(
        self,
        guard,
        *,
        status: RunReleaseStatus,
        job_status: JobQueueStatus | None = None,
        recovery_phase: str | None = None,
        retry_delay_ms=0,
        error_code=None,
    ):
        async with self.backend.create_uow().begin_write() as uow:
            await self.backend.jobs.release(
                uow,
                guard,
                status=status,
                job_status=job_status,
                recovery_phase=recovery_phase,
                retry_delay_ms=retry_delay_ms,
                error_code=error_code,
            )

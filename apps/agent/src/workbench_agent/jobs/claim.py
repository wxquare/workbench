"""Claim application service: owns the UoW, never emits driver SQL."""

from workbench_agent.domain.guards import ClaimedJob, WorkerCapabilities
from workbench_agent.jobs.fairness import Fairness


class JobService:
    def __init__(self, backend):
        self.backend = backend
        self.fairness = Fairness()
        self._scan_after = None
        self._scan_capabilities = None

    async def claim(self, capabilities: WorkerCapabilities, worker_id: str) -> ClaimedJob | None:
        if not worker_id or len(worker_id) > 128:
            raise ValueError('worker identity required')
        if capabilities != self._scan_capabilities:
            self._scan_after = None
            self._scan_capabilities = capabilities
        async with self.backend.create_uow().begin_write() as uow:
            candidates = await self.backend.jobs.candidates(uow, after=self._scan_after)
            owned, candidate = await self.backend.jobs.claim(
                uow, capabilities, worker_id, self.fairness.ordered(candidates)
            )
        # One bounded page per scan; rejected pages cannot hide later work forever.
        # Cursor is advisory, never ownership. Capability changes reconsider from
        # the front immediately, including previously blocked jobs.
        self._scan_after = (
            None
            if owned or not candidates
            else tuple(
                candidates[-1][key] for key in ('_level', '_team_rank', 'created_at', 'job_id')
            )
        )
        if owned:
            self.fairness.claimed(candidate)
        return owned

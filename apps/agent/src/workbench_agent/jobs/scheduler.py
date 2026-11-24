"""Database recovery/deadline scheduler with durable inspection fires."""

import asyncio
from dataclasses import dataclass
from workbench_agent.jobs.deadlines import DeadlineService
from workbench_agent.jobs.reconciler import Reconciler
from workbench_agent.persistence.errors import StorageError


@dataclass(frozen=True)
class TickReport:
    scanned: int = 0
    recovered: int = 0
    expired: int = 0
    unavailable: int = 0
    scheduled: int = 0
    schedule_unavailable: int = 0


class Scheduler:
    def __init__(self, backend, registry, *, connector_policy=None):
        self.backend, self.registry = backend, registry
        self.reconciler = Reconciler(backend, registry)
        self.reconciler.connector_policy = connector_policy
        from workbench_agent.jobs.schedules import ScheduleScanner

        self.schedule_scanner = ScheduleScanner(backend)
        self.stopping = asyncio.Event()
        self.after = None

    async def tick(self) -> TickReport:
        from workbench_agent.observability import span_or_none

        with span_or_none(self.backend, 'scheduler.tick', root=True):
            return await self._tick()

    async def _tick(self) -> TickReport:
        expired = await DeadlineService(self.backend).scan()
        async with self.backend.create_uow().begin_read() as uow:
            rows = await self.backend.jobs.candidates(uow, after=self.after)
        self.after = (
            tuple(rows[-1][key] for key in ('_level', '_team_rank', 'created_at', 'job_id'))
            if rows
            else None
        )
        recovered = unavailable = 0
        for job in rows:
            # Fresh ready execution belongs to Worker. Only expired ownership or
            # explicit durable projection intent is a recovery candidate.
            async with self.backend.create_uow().begin_read() as uow:
                run = await self.backend.runs.one(uow, 'runs', run_id=job['run_id'])
            if (
                job['status'] != 'leased'
                and run['recovery_phase'] not in ('projection', 'finalizing')
                and job['attempt'] < 3
            ):
                continue
            result = await self.reconciler.recover(job['run_id'])
            if result.action == 'unavailable':
                unavailable += 1
            else:
                recovered += 1
        schedules = await self.schedule_scanner.tick()
        return TickReport(
            len(rows),
            recovered,
            expired,
            unavailable,
            sum(item['status'] == 'started' for item in schedules),
            sum(item['status'] == 'unavailable' for item in schedules),
        )

    async def run(self):
        while not self.stopping.is_set():
            try:
                await self.tick()
            except StorageError:
                pass
            try:
                await asyncio.wait_for(self.stopping.wait(), 5)
            except TimeoutError:
                pass

    def stop(self):
        self.stopping.set()

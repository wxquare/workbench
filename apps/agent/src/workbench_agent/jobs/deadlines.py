"""Durable deadlines. Running expiration requires actual lease ownership."""

from workbench_agent.runtime.graphs.ids import GraphId


class DeadlineService:
    def __init__(self, backend, connector_policy=None):
        self.backend, self.connector_policy = backend, connector_policy

    @staticmethod
    def reason(run, now):
        # The immutable total lifetime wins if active/wait deadlines also pass.
        if run['wall_deadline_at'] <= now:
            return 'total_wall_expired'
        if (
            run['status'] in ('waiting_input', 'waiting_approval')
            and run['waiting_deadline_at'] is not None
            and run['waiting_deadline_at'] <= now
        ):
            return 'wait_expired'
        if (
            run['status'] in ('running', 'finalizing', 'cancel_requested')
            and run['deadline_at'] is not None
            and run['deadline_at'] <= now
        ):
            return 'active_budget_exhausted'
        raise ValueError('Run has no elapsed deadline')

    @classmethod
    def status(cls, run, now):
        # T08 supplies validated incomplete Outcomes for active-budget partial.
        # Queue/wait/48h lifetime expiration is expired, regardless of progress.
        return 'timed_out' if cls.reason(run, now) == 'active_budget_exhausted' else 'expired'

    async def scan(self):
        """Expire unleased logical waits/queue entries; never invalidate an owner.

        T09 must claim/take over an executing Run before expire_owned, even after
        its lease expires. A scanner does not gain authority just by observing it.
        """
        async with self.backend.create_uow().begin_write() as uow:
            rows = await self.backend.controls.due(uow)
            now = await self.backend.clock.now_ms(uow)
            count = 0
            for run in rows:
                if run['status'] not in ('queued', 'waiting_input', 'waiting_approval'):
                    continue
                if (
                    await self.backend.runs.active_until(uow, run['team_id'], run['run_id'])
                    is not None
                ):
                    continue
                await self.backend.controls.finish(uow, run, self.status(run, now))
                count += 1
            return count

    async def expire_owned(self, guard):
        async with self.backend.create_uow().begin_write() as uow:
            await self.backend.jobs.lock_scope(uow, guard)
            await self.backend.lease_guard.require(
                uow, guard, frozenset({'running', 'finalizing', 'cancel_requested'})
            )
            rows = await self.backend.controls.due(uow)
            run = next(
                (r for r in rows if r['team_id'] == guard.team_id and r['run_id'] == guard.run_id),
                None,
            )
            if run is None:
                return False
            now = await self.backend.clock.now_ms(uow)
            if (
                self.reason(run, now) == 'active_budget_exhausted'
                and run['status'] != 'cancel_requested'
            ):
                from workbench_agent.runtime.results import saved_outcome
                from workbench_agent.domain.context import RunContext
                from workbench_agent.application.results import ResultService
                from workbench_agent.application.errors import DomainError
                from pydantic import ValidationError
                from workbench_agent.jobs.worker import CapabilityBlocked

                saved = await self.backend.runs.checkpoint(uow, guard.team_id, guard.run_id)
                service = ResultService(self.backend)
                try:
                    context = RunContext.model_validate_json(run['versions'])
                    if context.graph_id in (
                        GraphId.RCA_SYNTHETIC,
                        GraphId.RCA_PROPOSAL,
                        GraphId.RCA_BATCH_PROPOSAL,
                    ):
                        from workbench_agent.application.synthetic_source_policy import (
                            require_bound_source,
                        )

                        identity = await require_bound_source(uow, context, guard.run_id)
                        check = getattr(self.connector_policy, 'require_source_current', None)
                        if not identity or not callable(check):
                            raise CapabilityBlocked('source identity policy unavailable')
                        await check(context, identity)
                    outcome = saved_outcome(saved, context.graph_id)
                    if outcome is not None:
                        from types import SimpleNamespace
                        from workbench_agent.runtime.graphs.evidence_authority import (
                            sanitize_v8_outcome,
                        )

                        if context.graph_id not in (
                            GraphId.RCA_SYNTHETIC,
                            GraphId.RCA_PROPOSAL,
                            GraphId.RCA_BATCH_PROPOSAL,
                        ):
                            outcome = await sanitize_v8_outcome(
                                self.backend,
                                uow,
                                SimpleNamespace(guard=guard, context=context),
                                outcome,
                            )
                        if context.graph_id not in (
                            GraphId.RCA_SYNTHETIC,
                            GraphId.RCA_PROPOSAL,
                            GraphId.RCA_BATCH_PROPOSAL,
                        ):
                            outcome = await service.validate(uow, guard, outcome)
                except (ValueError, ValidationError, DomainError, CapabilityBlocked):
                    outcome = None
                if outcome is not None:
                    await service.finalize_in(uow, guard, outcome, outcome.digest())
                    return True
            await self.backend.controls.finish(uow, run, self.status(run, now))
            return True

"""Same-UoW result, final event, message and guarded terminal release."""

from workbench_agent.runtime.graphs.ids import GraphId
from uuid import UUID, uuid5
from workbench_agent.domain.context import RunContext
from workbench_agent.domain.guards import ExecutionGuard, TERMINAL_RUN_STATUSES
from workbench_agent.domain.outcomes import RunOutcome
from workbench_agent.domain.runs import RunView
from workbench_agent.application.context import ContextResolver
from workbench_agent.application.errors import DomainError, not_found
from workbench_agent.application.idempotency import canonical
from workbench_agent.application.runs import RunService


class ResultService:
    def __init__(self, backend):
        self.backend = backend

    @staticmethod
    def _safe_partial(outcome):
        value = outcome.model_dump(mode='json')
        value.update(
            completed=False,
            summary='Analysis is incomplete because the alert scope is unavailable or changed.',
            findings=[],
            root_cause_hypotheses=[],
            impact=None,
            evidence_refs=[],
            proposed_actions=[],
            knowledge_gaps=[],
            unresolved_items=['The signed alert scope is unavailable or changed.'],
        )
        return RunOutcome.model_validate(value)

    async def validate(self, uow, guard, outcome):
        # Revalidate even model_copy/construct inputs; frozen DTO is not an authority.
        outcome = RunOutcome.model_validate(
            outcome.model_dump(mode='json') if isinstance(outcome, RunOutcome) else outcome
        )
        refs = {str(x) for x in outcome.evidence_refs}
        rows = (
            await self.backend.results.evidence(uow, guard.team_id, guard.run_id, refs)
            if refs
            else []
        )
        scope = await self.backend.runs.current_scope(uow, guard.team_id, guard.run_id)
        if {r['evidence_id'] for r in rows} != refs or any(
            scope is None or r['scope_id'] != scope['scope_id'] for r in rows
        ):
            raise DomainError('INVALID_EVIDENCE_SCOPE', 422)
        qualities = {r['evidence_id']: r['quality'] for r in rows}
        if any(
            f.status == 'normal' and any(qualities[str(ref)] != 'valid' for ref in f.evidence_refs)
            for f in outcome.findings
        ):
            raise DomainError('INVALID_NORMAL_EVIDENCE', 422)
        return outcome

    async def finalize(self, guard: ExecutionGuard, outcome: RunOutcome, digest: str) -> RunView:
        async with self.backend.create_uow().begin_write() as uow:
            return await self.finalize_in(uow, guard, outcome, digest)

    async def finalize_in(self, uow, guard, outcome, digest):
        run = await self.backend.runs.one(uow, 'runs', team_id=guard.team_id, run_id=guard.run_id)
        if run is None:
            raise not_found()
        # Replays use the durable pinned identity and current membership, not a dead lease.
        context = RunContext.model_validate_json(run['versions'])
        auth = await ContextResolver(self.backend).revalidate(context, uow=uow)
        outcome = RunOutcome.model_validate(
            outcome.model_dump(mode='json') if isinstance(outcome, RunOutcome) else outcome
        )
        if outcome.digest() != digest:
            raise DomainError('RESULT_DIGEST_MISMATCH', 409)
        candidate_digest = digest
        if context.graph_id in (
            GraphId.RCA_SYNTHETIC,
            GraphId.RCA_PROPOSAL,
            GraphId.RCA_BATCH_PROPOSAL,
        ):
            from workbench_agent.application.synthetic_source_policy import require_bound_source

            await require_bound_source(uow, context, guard.run_id)
        saved = await self.backend.results.get(uow, guard.team_id, guard.run_id)
        if saved:
            if context.graph_id in (
                GraphId.RCA_SYNTHETIC,
                GraphId.RCA_PROPOSAL,
                GraphId.RCA_BATCH_PROPOSAL,
            ):
                from sqlalchemy import select
                from workbench_agent.persistence import schema

                table = schema.synthetic_result_bindings
                binding = (
                    (
                        await uow.execute(
                            select(table).where(
                                table.c.team_id == guard.team_id, table.c.run_id == guard.run_id
                            )
                        )
                    )
                    .mappings()
                    .first()
                )
                if (
                    not binding
                    or binding['candidate_digest'] != candidate_digest
                    or binding['projection_digest'] != saved['digest']
                ):
                    raise DomainError('RESULT_CONFLICT', 409)
            elif saved['digest'] != digest:
                raise DomainError('RESULT_CONFLICT', 409)
            if run['status'] not in TERMINAL_RUN_STATUSES or not saved['response']:
                raise DomainError('RESULT_INCONSISTENT', 409)
            # Re-project current disclosure on replay; stored response may predate revocation.
            return await RunService(self.backend).view(uow, auth, guard.run_id)
        if run['status'] in TERMINAL_RUN_STATUSES:
            raise DomainError('RESULT_CONFLICT', 409)
        await self.backend.jobs.lock_scope(uow, guard)
        await self.backend.lease_guard.require(uow, guard)
        if context.graph_id == GraphId.KNOWLEDGE_CITATION_REVIEW:
            from types import SimpleNamespace
            from workbench_agent.runtime.graphs.evidence_authority import sanitize_v8_outcome

            outcome = await sanitize_v8_outcome(
                self.backend, uow, SimpleNamespace(guard=guard, context=context), outcome
            )
            digest = outcome.digest()
        if context.graph_id in (
            GraphId.RCA_SYNTHETIC,
            GraphId.RCA_PROPOSAL,
            GraphId.RCA_BATCH_PROPOSAL,
        ):
            from types import SimpleNamespace
            from workbench_agent.runtime.graphs.evidence_authority import sanitize_v8_outcome
            from workbench_agent.incidents.synthetic import source_scope

            outcome = await sanitize_v8_outcome(
                self.backend, uow, SimpleNamespace(guard=guard, context=context), outcome
            )
            _, _, _, matches = await source_scope(self.backend, uow, guard)
            if not matches:
                outcome = self._safe_partial(outcome)
            digest = outcome.digest()
        outcome = await self.validate(uow, guard, outcome)
        now = await self.backend.clock.now_ms(uow)
        # Lifetime always wins. Active-budget projection may preserve valid progress.
        if run['wall_deadline_at'] <= now:
            raise DomainError('RUN_EXPIRED', 409)
        result_id = str(uuid5(UUID(guard.run_id), 'result:1'))
        message_id = str(uuid5(UUID(guard.run_id), 'final:1'))
        await self.backend.runs.change(
            uow,
            'runs',
            {'team_id': guard.team_id, 'run_id': guard.run_id},
            {'status': 'finalizing', 'outcome_ref': result_id},
        )
        await self.backend.runs.insert(
            uow,
            'messages',
            dict(
                team_id=guard.team_id,
                message_id=message_id,
                thread_id=guard.thread_id,
                run_id=guard.run_id,
                role='assistant',
                content_ref=canonical({'outcome': outcome.model_dump(mode='json')}),
                created_at=now,
                updated_at=now,
            ),
        )
        await self.backend.results.insert(
            uow,
            dict(
                team_id=guard.team_id,
                run_id=guard.run_id,
                result_id=result_id,
                digest=digest,
                payload=outcome.canonical(),
                message_id=message_id,
                created_at=now,
            ),
        )
        if context.graph_id in (
            GraphId.RCA_SYNTHETIC,
            GraphId.RCA_PROPOSAL,
            GraphId.RCA_BATCH_PROPOSAL,
        ):
            from sqlalchemy import insert
            from workbench_agent.persistence import schema

            await uow.execute(
                insert(schema.synthetic_result_bindings).values(
                    team_id=guard.team_id,
                    run_id=guard.run_id,
                    candidate_digest=candidate_digest,
                    projection_digest=digest,
                )
            )
            from workbench_agent.incidents.synthetic import project_incident

            await project_incident(self.backend, uow, guard, run, now)
        await self.backend.execution_events.supersede_drafts(
            uow, guard, final_message_id=message_id
        )
        status = 'succeeded' if outcome.completed else 'partial'
        await self.backend.execution_events.append_execution(
            uow,
            guard,
            'run.completed',
            {
                'terminal_status': status,
                'result_id': result_id,
                'summary': outcome.summary,
                'links': {'result': f'/v1/runs/{guard.run_id}'},
            },
            message_id=message_id,
        )
        await self.backend.jobs.release(uow, guard, status=status)
        view = await RunService(self.backend).view(uow, auth, guard.run_id)
        await self.backend.results.response(
            uow, guard.team_id, guard.run_id, canonical(view.model_dump(mode='json'))
        )
        return view

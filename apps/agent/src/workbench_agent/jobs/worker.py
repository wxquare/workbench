"""Cooperative worker. Graphs and guarded lifecycle projectors are injected.

An executor must call execution.check_operation() before each new external
operation and propagate cancellation. It must not add retries around invokers.
A successful return is not a durable Run outcome: only a registered projector
may apply checkpoint-derived waiting/result state in the same fenced UoW.
"""

from workbench_agent.runtime.graphs.ids import GraphId
import asyncio
import random
import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Protocol, Any, Callable, Awaitable
from workbench_agent.domain.admin_jobs import ClaimedAdminJob, ADMIN_JOB_KINDS
from workbench_agent.application.context import ContextResolver
from workbench_agent.application.errors import DomainError
from workbench_agent.domain.guards import ClaimedJob, WorkerCapabilities, EXECUTING_RUN_STATUSES
from workbench_agent.jobs.claim import JobService
from workbench_agent.jobs.leases import LeaseService
from workbench_agent.persistence.errors import StorageError, StaleExecution
from workbench_agent.observability import bind_context, current_telemetry, span_or_none


class InfrastructureFailure(Exception):
    """Retry only a safe infrastructure segment; never an uncertain action."""


class ProjectionFailure(Exception):
    """Execution progress exists; recover projection without replaying the executor."""


class CapabilityBlocked(Exception):
    pass


async def _stop_attempt(execution, attempt):
    execution.cancelled.set()
    attempt.cancel()
    await asyncio.gather(attempt, return_exceptions=True)


class Executor(Protocol):
    async def execute(self, execution: 'Execution') -> Any: ...


class LifecycleProjector(Protocol):
    async def project(self, uow, claim: ClaimedJob, result: Any) -> None:
        """Use caller UoW, fence, persist outcome/interrupt, then release lifecycle."""
        ...


class Reconciler(Protocol):
    async def reconcile(self, uow, claim: ClaimedJob) -> None:
        """Projection ONLY: inspect persisted progress, never run graph or call network.

        T09 projects valid outcome/interrupt first, else retry-exhausted failure.
        Release ownership/quota/logical slot only through guarded lifecycle writes.
        """
        ...


class ConnectorPolicy(Protocol):
    async def require_current(self, context) -> None: ...


class DeploymentConnectorPolicy:
    """Consult current registered deployment bounds on every new operation.

    Provider may reload configuration; immutable Run scope remains a further
    restriction, never authority to restore a revoked connector binding.
    """

    def __init__(self, provider):
        self.provider = provider

    async def require_current(self, context):
        allowed = any(
            binding['subject'] == context.subject
            and binding['team_id'] == context.team_id
            and context.agent_id in binding['agent_ids']
            for binding in self.provider().values()
        )
        if not allowed:
            raise CapabilityBlocked('current connector deployment policy revoked')

    async def require_source_current(self, context, identity_id):
        binding = self.provider().get(identity_id)
        if (
            not binding
            or binding['subject'] != context.subject
            or binding['team_id'] != context.team_id
            or context.agent_id not in binding['agent_ids']
        ):
            raise CapabilityBlocked('source connector deployment policy revoked')


@dataclass
class Execution:
    backend: Any
    claim: ClaimedJob
    connector_policy: ConnectorPolicy | None = None
    tool_permissions: Any = None
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)
    executor_started: bool = field(default=False, init=False)
    executor_completed: bool = field(default=False, init=False)
    _tool_owner: Any = field(default=None, init=False, repr=False)
    _expert_name: str = field(default='diagnosis', init=False, repr=False)

    @property
    def guard(self):
        return self.claim.guard

    async def check_operation(self):
        """New external execution is allowed only while the Run is running."""
        await self._require_current(frozenset({'running'}))
        async with self.backend.create_uow().begin_read() as uow:
            run = await self.backend.runs.one(
                uow, 'runs', team_id=self.guard.team_id, run_id=self.guard.run_id
            )
            now = await self.backend.clock.now_ms(uow)
            if (
                run['status'] != 'running'
                or run['run_epoch'] != self.guard.run_epoch
                or self.cancelled.is_set()
                or run['wall_deadline_at'] <= now
                or run['deadline_at'] is not None
                and run['deadline_at'] <= now
            ):
                raise StaleExecution()

    async def check_projection(self):
        """Validate persistence-only recovery, including an already finalizing Run."""
        await self._require_current(EXECUTING_RUN_STATUSES)

    async def _require_current(self, allowed_statuses):
        if self.cancelled.is_set():
            raise asyncio.CancelledError()
        source_identity = None
        async with self.backend.create_uow().begin_write() as uow:
            await self.backend.lease_guard.require(uow, self.guard, allowed_statuses)
            await ContextResolver(self.backend).revalidate(self.claim.context, uow=uow)
            if self.claim.context.graph_id in (
                GraphId.RCA_SYNTHETIC,
                GraphId.RCA_PROPOSAL,
                GraphId.RCA_BATCH_PROPOSAL,
            ):
                from workbench_agent.application.synthetic_source_policy import require_bound_source

                try:
                    source_identity = await require_bound_source(
                        uow, self.claim.context, self.guard.run_id
                    )
                except DomainError:
                    raise CapabilityBlocked('source policy revoked') from None
        if self.claim.context.identity_kind == 'connector':
            if self.connector_policy is None:
                raise CapabilityBlocked('current connector deployment policy unavailable')
            if self.claim.context.graph_id in (
                GraphId.RCA_SYNTHETIC,
                GraphId.RCA_PROPOSAL,
                GraphId.RCA_BATCH_PROPOSAL,
            ):
                require_source = getattr(self.connector_policy, 'require_source_current', None)
                if not callable(require_source):
                    raise CapabilityBlocked('source identity policy unavailable')
                await require_source(self.claim.context, source_identity)
            else:
                await self.connector_policy.require_current(self.claim.context)
        if self.cancelled.is_set():
            raise asyncio.CancelledError()


class ExecutorRegistry:
    def __init__(self, *, features=frozenset()):
        self.features = frozenset(features)
        self._executors: dict[str, Executor] = {}
        self._projectors: dict[str, LifecycleProjector] = {}
        self.admin: dict[str, Callable[[ClaimedAdminJob], Awaitable[str]]] = {}
        self.reconciler: Reconciler | None = None

    def register(
        self, graph_id: str, executor: Executor, projector: LifecycleProjector | None = None
    ):
        if graph_id in self._executors:
            raise ValueError('pinned graph implementation already registered')
        self._executors[graph_id] = executor
        if projector:
            self._projectors[graph_id] = projector

    def register_admin(self, kind, executor):
        if kind not in ADMIN_JOB_KINDS or kind in self.admin:
            raise ValueError('unsupported or duplicate admin capability')
        self.admin[kind] = executor

    def capabilities(self):
        return WorkerCapabilities(
            graph_ids=tuple(key for key in self._executors if key in self._projectors),
            projection_graph_ids=tuple(self._executors) if self.reconciler is not None else (),
            features=self.features,
            admin_kinds=frozenset(self.admin) & ADMIN_JOB_KINDS,
            can_reconcile=self.reconciler is not None,
        )


class Worker:
    def __init__(
        self,
        backend,
        worker_id,
        registry: ExecutorRegistry,
        *,
        renew_interval=10,
        scan_interval=5,
        connector_policy=None,
        jitter=None,
    ):
        self.backend, self.worker_id, self.registry = backend, worker_id, registry
        self.jobs, self.leases = JobService(backend), LeaseService(backend)
        self.renew_interval, self.scan_interval = renew_interval, scan_interval
        self.connector_policy = connector_policy
        self.tool_permissions = getattr(backend, '_tool_permissions', None)
        self.jitter = jitter or (lambda: random.uniform(0, 0.5))
        self.stopping = asyncio.Event()
        self._admin_lock = asyncio.Lock()  # Independent, small management quota = 1
        self._tool_executions = {}
        self._active_tool_claims = {}

    def _bind_tool_execution(self, claim):
        if (
            self._active_tool_claims.get(id(claim)) is not claim
            or claim.guard.worker_id != self.worker_id
        ):
            raise PermissionError('Worker does not own active claim')
        execution = Execution(self.backend, claim, self.connector_policy, self.tool_permissions)
        if claim.context.graph_id in (
            GraphId.KNOWLEDGE_SCOPED_EXCERPT,
            GraphId.KNOWLEDGE_CITATION_REVIEW,
            GraphId.CONVERSATION_TOOL_USE,
        ):
            execution._expert_name = 'coordinator'
        execution._tool_owner = self
        self._tool_executions[id(execution)] = (
            execution,
            execution.tool_permissions,
            execution._expert_name,
            execution.claim,
        )
        return execution

    def _owns_tool_execution(self, execution):
        recorded = self._tool_executions.get(id(execution))
        return bool(
            recorded
            and recorded[0] is execution
            and recorded[1] is execution.tool_permissions
            and recorded[1] is self.tool_permissions
            and recorded[1] is getattr(self.backend, '_tool_permissions', None)
            and recorded[2] == execution._expert_name
            and recorded[3] is execution.claim
            and execution._tool_owner is self
        )

    @contextmanager
    def expert_execution(self, parent, name):
        """Issue narrowly scoped child authority only for a live pinned coordinator Run."""
        if (
            not self._owns_tool_execution(parent)
            or parent._expert_name != 'coordinator'
            or parent.claim.context.graph_id
            not in (
                GraphId.KNOWLEDGE_SCOPED_EXCERPT,
                GraphId.KNOWLEDGE_CITATION_REVIEW,
                GraphId.CONVERSATION_TOOL_USE,
            )
            or name not in {'knowledge', 'code'}
            or parent.claim.context.graph_id
            in (
                GraphId.KNOWLEDGE_SCOPED_EXCERPT,
                GraphId.KNOWLEDGE_CITATION_REVIEW,
                GraphId.CONVERSATION_TOOL_USE,
            )
            and name != 'knowledge'
        ):
            raise PermissionError('trusted coordinator required')
        child = Execution(
            self.backend,
            parent.claim,
            self.connector_policy,
            self.tool_permissions,
            cancelled=parent.cancelled,
        )
        child._expert_name = name
        child._tool_owner = self
        self._tool_executions[id(child)] = (child, child.tool_permissions, name, child.claim)
        try:
            yield child
        finally:
            self._tool_executions.pop(id(child), None)

    async def _renew(self, execution):
        while True:
            await asyncio.sleep(self.renew_interval)
            # Finish a bounded storage cycle before cancellation closes the worker.
            # Cancelling the SQLAlchemy driver mid-close can strand bookkeeping.
            cycle = asyncio.create_task(self._renew_cycle(execution))
            try:
                keep = await asyncio.shield(cycle)
            except asyncio.CancelledError:
                await asyncio.gather(cycle, return_exceptions=True)
                raise
            if not keep:
                return

    async def _renew_cycle(self, execution):
        try:
            from workbench_agent.jobs.deadlines import DeadlineService

            await DeadlineService(
                self.backend, connector_policy=self.connector_policy
            ).expire_owned(execution.guard)
            async with self.backend.create_uow().begin_read() as uow:
                status = await self.backend.controls.status(
                    uow, execution.guard.team_id, execution.guard.run_id
                )
            if status != 'cancel_requested' and await self.leases.renew(execution.guard):
                return True
        except StorageError:
            pass
        execution.cancelled.set()
        return False

    async def _project(self, claim, result):
        projector = self.registry._projectors.get(claim.context.graph_id)
        if projector is None:
            raise CapabilityBlocked('guarded lifecycle projector unavailable')
        async with self.backend.create_uow().begin_write() as uow:
            await self.backend.jobs.lock_scope(uow, claim.guard)
            await self.backend.lease_guard.require(uow, claim.guard, EXECUTING_RUN_STATUSES)
            await projector.project(uow, claim, result)

    async def _execute(self, execution):
        claim = execution.claim
        if await self._finish_cancel(claim):
            return
        await (
            execution.check_projection() if claim.projection_only else execution.check_operation()
        )
        if claim.projection_only:
            if self.registry.reconciler is None:
                raise CapabilityBlocked('projection-only reconciler unavailable')
            observation = (
                await self.registry.reconciler.prepare(claim)
                if hasattr(self.registry.reconciler, 'prepare')
                else None
            )
            try:
                async with self.backend.create_uow().begin_write() as uow:
                    await self.backend.jobs.lock_scope(uow, claim.guard)
                    await self.backend.lease_guard.require(uow, claim.guard, EXECUTING_RUN_STATUSES)
                    if hasattr(self.registry.reconciler, 'prepare'):
                        await self.registry.reconciler.reconcile(uow, claim, observation)
                    else:
                        await self.registry.reconciler.reconcile(uow, claim)
            except StaleExecution:
                raise
            except Exception:
                raise ProjectionFailure() from None
            return
        if claim.context.graph_id not in self.registry._projectors:
            raise CapabilityBlocked('guarded lifecycle projector unavailable')
        execution.executor_started = True
        with span_or_none(
            self.backend, 'graph.execute', attributes={'workbench.graph_id': claim.context.graph_id}
        ):
            result = await self.registry._executors[claim.context.graph_id].execute(execution)
        execution.executor_completed = True
        try:
            await execution.check_projection()
            await self._project(claim, result)
        except StaleExecution:
            raise
        except Exception:
            raise ProjectionFailure() from None

    async def run_once(self):
        claim = await self.jobs.claim(self.registry.capabilities(), self.worker_id)
        if claim is None:
            return 'idle' if self.registry.capabilities().graph_ids else 'capability_blocked'
        telemetry = current_telemetry(self.backend)
        parent = None
        if telemetry is not None and telemetry.tracer is not None:
            from sqlalchemy import select
            from workbench_agent.persistence import schema

            async with self.backend.create_uow().begin_read() as uow:
                parent = (
                    await uow.execute(
                        select(schema.run_trace_contexts.c.traceparent).where(
                            schema.run_trace_contexts.c.team_id == claim.guard.team_id,
                            schema.run_trace_contexts.c.run_id == claim.guard.run_id,
                        )
                    )
                ).scalar_one_or_none()
        guard = claim.guard
        with bind_context(
            team_id=guard.team_id,
            agent_id=claim.context.agent_id,
            thread_id=guard.thread_id,
            run_id=guard.run_id,
            job_id=guard.job_id,
            run_epoch=guard.run_epoch,
            attempt=claim.attempt,
            graph_id=claim.context.graph_id,
        ):
            with span_or_none(
                self.backend,
                'job.execute',
                traceparent=parent,
                attributes={
                    'langfuse.observation.type': 'span',
                    'session.id': guard.thread_id,
                    'workbench.team_id': guard.team_id,
                    'workbench.agent_id': claim.context.agent_id,
                    'workbench.run_id': guard.run_id,
                    'workbench.job_id': guard.job_id,
                    'workbench.run_epoch': guard.run_epoch,
                    'workbench.attempt': claim.attempt,
                },
            ):
                logging.getLogger('workbench.worker').info(
                    'job.claimed', extra={'event': 'job.claimed'}
                )
                status = await self._run_claim(claim)
                logging.getLogger('workbench.worker').info(
                    'job.finished', extra={'event': 'job.finished', 'status': status}
                )
                return status

    async def _run_claim(self, claim):
        self._active_tool_claims[id(claim)] = claim
        execution = self._bind_tool_execution(claim)
        attempt = asyncio.create_task(self._execute(execution))
        renewal = asyncio.create_task(self._renew(execution))
        try:
            done, _ = await asyncio.wait({attempt, renewal}, return_when=asyncio.FIRST_COMPLETED)
            if renewal in done:
                # Even a concurrent executor return cannot project after lost ownership.
                await _stop_attempt(execution, attempt)
                if await self._finish_cancel(claim):
                    return 'cancelled'
                return 'lease_lost'
            await attempt
            return 'projected'
        except StaleExecution:
            if await self._finish_cancel(claim):
                return 'cancelled'
            return 'stale_execution'
        except CapabilityBlocked:
            if execution.executor_started or claim.projection_only:
                await self._release_if_owned(
                    claim,
                    status='queued',
                    job_status='retry_wait',
                    recovery_phase='projection',
                    error_code=claim.reconciliation_reason or 'EXECUTION_CAPABILITY_LOST',
                )
                return 'recovery_scheduled'
            await self._release_if_owned(claim, status='queued', job_status='capability_blocked')
            return 'capability_blocked'
        except ProjectionFailure:
            await self._release_if_owned(
                claim,
                status='queued',
                job_status='retry_wait',
                recovery_phase='projection',
                error_code='PROJECTION_FAILED',
            )
            return 'recovery_scheduled'
        except InfrastructureFailure:
            if claim.segment_attempt >= 3:
                # Existing checkpoint progress belongs to the projection-only reconciler.
                await self._release_if_owned(
                    claim, status='queued', job_status='retry_wait', error_code='RETRY_EXHAUSTED'
                )
            else:
                delay = (2 if claim.segment_attempt == 1 else 10) + self.jitter()
                await self._release_if_owned(
                    claim,
                    status='queued',
                    job_status='retry_wait',
                    retry_delay_ms=int(delay * 1000),
                )
            return 'retry_wait'
        except asyncio.CancelledError:
            await _stop_attempt(execution, attempt)
            await self._release_if_owned(
                claim,
                status='queued',
                job_status='retry_wait',
                recovery_phase='projection'
                if execution.executor_started or claim.projection_only
                else None,
                error_code='PROJECTION_CANCELLED'
                if execution.executor_started or claim.projection_only
                else None,
            )
            raise
        except Exception:
            if execution.executor_started or claim.projection_only:
                await self._release_if_owned(
                    claim,
                    status='queued',
                    job_status='retry_wait',
                    recovery_phase='projection',
                    error_code='EXECUTOR_FAILED',
                )
                return 'recovery_scheduled'
            # Preflight failed before the executor started; no external work replay.
            await self._release_if_owned(
                claim,
                status='queued',
                job_status='capability_blocked',
                error_code='EXECUTION_UNAVAILABLE',
            )
            return 'capability_blocked'
        finally:
            execution.cancelled.set()
            self._tool_executions.pop(id(execution), None)
            self._active_tool_claims.pop(id(claim), None)
            for task in (attempt, renewal):
                task.cancel()
            await asyncio.gather(attempt, renewal, return_exceptions=True)
            await self._finish_cancel(claim)

    async def _finish_cancel(self, claim):
        try:
            async with self.backend.create_uow().begin_write() as uow:
                status = await self.backend.controls.status(
                    uow, claim.guard.team_id, claim.guard.run_id
                )
                if status != 'cancel_requested':
                    return False
                await self.backend.controls.finish_cancel(uow, claim.guard)
            return True
        except (StaleExecution, StorageError):
            return False

    async def _release_if_owned(self, claim, **changes):
        try:
            await self.leases.release(claim.guard, **changes)
        except (StaleExecution, StorageError):
            pass

    async def run_admin_once(self):
        if not self.registry.admin or self._admin_lock.locked():
            return 'capability_blocked' if not self.registry.capabilities().graph_ids else 'idle'
        async with self._admin_lock:
            async with self.backend.create_uow().begin_write() as uow:
                if await self.backend.jobs.has_priority_work(uow, self.registry.capabilities()):
                    return 'run_priority'
                claim = await self.backend.admin_jobs.claim(
                    uow, set(self.registry.capabilities().admin_kinds), self.worker_id
                )
            if claim is None:
                return 'idle'

            async def renew():
                while True:
                    await asyncio.sleep(self.renew_interval)
                    try:
                        async with self.backend.create_uow().begin_write() as uow:
                            await self.backend.admin_jobs.renew(uow, claim)
                    except StorageError:
                        return

            work = asyncio.create_task(self.registry.admin[claim.kind](claim))
            renewal = asyncio.create_task(renew())
            try:
                done, _ = await asyncio.wait({work, renewal}, return_when=asyncio.FIRST_COMPLETED)
                if renewal in done:
                    return 'lease_lost'
                result = await work
                if not isinstance(result, str) or not result:
                    raise ValueError('admin executor requires a durable result reference')
                async with self.backend.create_uow().begin_write() as uow:
                    await self.backend.admin_jobs.finish(uow, claim, result)
                return 'admin_completed'
            except StaleExecution:
                return 'stale_execution'
            except Exception:
                # Keep the slot alive; never report an unprojected admin result.
                return 'admin_blocked'
            finally:
                work.cancel()
                renewal.cancel()
                await asyncio.gather(work, renewal, return_exceptions=True)

    async def run(self):
        async def slot(*, admin=False):
            while not self.stopping.is_set():
                try:
                    await (self.run_admin_once() if admin else self.run_once())
                except StorageError:
                    pass
                try:
                    await asyncio.wait_for(self.stopping.wait(), self.scan_interval)
                except TimeoutError:
                    pass

        slots = [asyncio.create_task(slot()) for _ in range(2)]
        slots.append(asyncio.create_task(slot(admin=True)))
        try:
            await self.stopping.wait()
            _, pending = await asyncio.wait(slots, timeout=30)
            for task in pending:
                task.cancel()
            await asyncio.gather(*slots, return_exceptions=True)
        finally:
            for task in slots:
                task.cancel()
            await asyncio.gather(*slots, return_exceptions=True)

    def stop(self):
        self.stopping.set()

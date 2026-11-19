"""Immutable ownership tokens. A token is evidence only when fenced in a UoW."""
from dataclasses import dataclass
from typing import Literal
from workbench_agent.domain.context import RunContext

@dataclass(frozen=True)
class ExecutionGuard:
    team_id: str
    thread_id: str
    run_id: str
    job_id: str
    worker_id: str
    run_epoch: int

@dataclass(frozen=True)
class WorkerCapabilities:
    graph_ids: tuple[str, ...] = ()
    features: frozenset[str] = frozenset()
    admin_kinds: frozenset[str] = frozenset()
    can_reconcile: bool = False
    projection_graph_ids: tuple[str, ...] = ()

@dataclass(frozen=True)
class ClaimedJob:
    guard: ExecutionGuard
    context: RunContext
    kind: str
    generation: int
    attempt: int
    lease_until: int
    segment_attempt: int
    projection_only: bool = False
    reconciliation_reason: str | None = None

# Closed Run and Job protocol values; scheduling state is not a Run status.
RunReleaseStatus = Literal['queued','waiting_input','waiting_approval','succeeded','partial','failed','cancelled','timed_out','expired']
JobQueueStatus = Literal['ready','retry_wait','capability_blocked']
WAITING_RUN_STATUSES = frozenset({'waiting_input','waiting_approval'})
TERMINAL_RUN_STATUSES = frozenset({'succeeded','partial','failed','cancelled','timed_out','expired'})
EXECUTING_RUN_STATUSES = frozenset({'running','finalizing'})
PROJECTION_RECOVERY_PHASES = frozenset({'projection','finalizing'})

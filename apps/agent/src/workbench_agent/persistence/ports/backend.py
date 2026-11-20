from typing import Protocol
from workbench_agent.persistence.ports.checkpoints import CheckpointStorage
from workbench_agent.persistence.ports.execution import ExecutionJobStore, ExecutionLeaseGuard, ExecutionEventStore
from workbench_agent.persistence.ports.runs import RunStore
from workbench_agent.persistence.ports.configuration import ConfigurationStore
from workbench_agent.persistence.ports.uow import UnitOfWork, StorageClock
from workbench_agent.persistence.ports.stores import AdminJobStore

from workbench_agent.persistence.ports.controls import ControlStore

from workbench_agent.persistence.ports.results import ResultStore

from workbench_agent.persistence.ports.usage import UsageStore
from workbench_agent.persistence.ports.tools import ToolStore
from workbench_agent.persistence.ports.runner_cleanup import RunnerCleanupStore
from workbench_agent.persistence.ports.knowledge import KnowledgeStore
from workbench_agent.persistence.ports.statistics import StatisticsStore

class StorageBackend(Protocol):
    knowledge: KnowledgeStore
    statistics: StatisticsStore
    runner_cleanup: RunnerCleanupStore
    tools: ToolStore
    usage: UsageStore
    results: ResultStore
    controls: ControlStore
    checkpoints: CheckpointStorage
    jobs: ExecutionJobStore
    lease_guard: ExecutionLeaseGuard
    execution_events: ExecutionEventStore
    runs: RunStore
    configuration: ConfigurationStore
    clock: StorageClock
    admin_jobs: AdminJobStore
    capabilities: frozenset[str]
    def create_uow(self) -> UnitOfWork: ...
    async def health(self) -> bool: ...
    async def close(self) -> None: ...

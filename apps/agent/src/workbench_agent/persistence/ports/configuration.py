"""Portable configuration repository. All operations share the caller's UoW."""

from typing import Protocol, Any, Mapping
from workbench_agent.persistence.ports.uow import UnitOfWork

Row = Mapping[str, Any]


class ConfigurationStore(Protocol):
    async def one(self, uow: UnitOfWork, resource: str, **scope) -> Row | None: ...
    async def rows(
        self, uow: UnitOfWork, resource: str, *, limit: int = 100, after: str | None = None, **scope
    ) -> list[Row]: ...
    async def latest_workflow_version(
        self, uow: UnitOfWork, team_id: str, agent_id: str, name: str
    ) -> int: ...
    async def insert(self, uow: UnitOfWork, resource: str, values: dict) -> None: ...
    async def change(self, uow: UnitOfWork, resource: str, scope: dict, values: dict) -> bool: ...

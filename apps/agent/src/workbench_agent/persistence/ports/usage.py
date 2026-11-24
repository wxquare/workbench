"""Portable usage ledger; every write belongs to its caller's fenced UoW."""

from typing import Protocol, Mapping, Any, Sequence
from workbench_agent.persistence.ports.uow import UnitOfWork

Row = Mapping[str, Any]


class UsageStore(Protocol):
    async def policy(self, uow: UnitOfWork, team_id: str, run_id: str) -> Row | None: ...
    async def reservation(
        self, uow: UnitOfWork, team_id: str, run_id: str, invocation: str
    ) -> Row | None: ...
    async def record(
        self, uow: UnitOfWork, team_id: str, run_id: str, invocation: str
    ) -> Row | None: ...
    async def reservations(self, uow: UnitOfWork, team_id: str, run_id: str) -> Sequence[Row]: ...
    async def records(self, uow: UnitOfWork, team_id: str, run_id: str) -> Sequence[Row]: ...
    async def insert(self, uow: UnitOfWork, resource: str, values: Row) -> None: ...
    async def change(
        self,
        uow: UnitOfWork,
        resource: str,
        team_id: str,
        run_id: str,
        invocation: str,
        values: Row,
    ) -> None: ...
    async def quarantined(
        self, uow: UnitOfWork, team_id: str, run_id: str, invocation: str
    ) -> Sequence[Row]: ...

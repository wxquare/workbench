"""Read-only projections over existing Run, Interrupt, Action and Outbox state."""
from typing import Protocol

from workbench_agent.persistence.ports.uow import UnitOfWork


class ReviewStore(Protocol):
    async def candidates(self, uow: UnitOfWork, auth, state: str) -> dict: ...

from typing import Protocol


class StatisticsStore(Protocol):
    async def summary(
        self,
        uow,
        *,
        team_id: str,
        agent_ids: tuple[str, ...],
        start_ms: int,
        end_ms: int,
    ) -> dict: ...

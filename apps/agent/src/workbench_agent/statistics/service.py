"""Application authorization before statistics SQL aggregation."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from workbench_agent.application.authorization import Authorization
from workbench_agent.application.errors import DomainError, not_found
from workbench_agent.domain.identity import ROLE_LEVEL
from workbench_agent.persistence import schema


class StatisticsService:
    def __init__(self, backend):
        self.backend = backend

    async def summary(self, auth, *, start_at: str, end_at: str, agent_id=None) -> dict:
        start = self._parse(start_at)
        end = self._parse(end_at)
        if not start < end or end - start > 90 * 24 * 3600 * 1000:
            raise DomainError('INVALID_STATISTICS_RANGE', 422)
        if auth.agent_scope is not None or auth.identity_kind == 'connector':
            raise not_found()
        async with self.backend.create_uow().begin_read() as uow:
            current = await Authorization(self.backend).current(uow, auth)
            if ROLE_LEVEL[current.roles[0]] < ROLE_LEVEL['maintainer']:
                raise DomainError('FORBIDDEN', 403)
            query = select(schema.agents.c.agent_id).where(
                schema.agents.c.team_id == auth.team_id,
                schema.agents.c.status == 'active',
            )
            if agent_id:
                query = query.where(schema.agents.c.agent_id == agent_id)
            agent_ids = tuple((await uow.execute(query)).scalars().all())
            if agent_id and not agent_ids:
                raise not_found()
            data = await self.backend.statistics.summary(
                uow,
                team_id=auth.team_id,
                agent_ids=agent_ids,
                start_ms=start,
                end_ms=end,
            )
        return {
            'generated_at': datetime.now(timezone.utc).isoformat(),
            'cohort': {
                'start_at': start_at,
                'end_at': end_at,
                'agent_id': agent_id,
            },
            'definitions_version': 'stats-v1',
            **data,
        }

    @staticmethod
    def _parse(value: str) -> int:
        try:
            parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except (ValueError, TypeError):
            raise DomainError('INVALID_STATISTICS_RANGE', 422) from None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise DomainError('INVALID_STATISTICS_RANGE', 422)
        return int(parsed.timestamp() * 1000)

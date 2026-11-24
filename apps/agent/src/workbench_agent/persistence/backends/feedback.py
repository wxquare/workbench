"""Persistence adapter for append-only user feedback."""

from sqlalchemy import insert, select
from workbench_agent.persistence import schema


class SQLiteFeedbackStore:
    async def append(self, uow, values):
        await uow.execute(insert(schema.feedback).values(**values))

    async def list_for_run(self, uow, team_id, run_id, limit, after):
        table = schema.feedback
        query = select(table).where(table.c.team_id == team_id, table.c.run_id == run_id)
        if after is not None:
            query = query.where(table.c.feedback_id > after)
        return list(
            (await uow.execute(query.order_by(table.c.feedback_id).limit(limit + 1))).mappings()
        )

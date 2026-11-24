"""SQL usage adapter. No lifecycle timing or hidden transactions live here."""

from sqlalchemy import select, insert, update
from workbench_agent.persistence import schema as s

TABLES = {
    name: getattr(s, name)
    for name in ('usage_reservations', 'usage_records', 'usage_quarantine', 'usage_budget_policies')
}


class SQLiteUsageStore:
    @staticmethod
    def scope(table, team_id, run_id, invocation=None):
        clauses = [table.c.team_id == team_id, table.c.run_id == run_id]
        if invocation is not None:
            clauses.append(table.c.invocation == invocation)
        return clauses

    async def _rows(self, uow, name, team_id, run_id, invocation=None):
        table = TABLES[name]
        return list(
            (
                await uow.execute(
                    select(table).where(*self.scope(table, team_id, run_id, invocation))
                )
            ).mappings()
        )

    async def policy(self, uow, team_id, run_id):
        rows = await self._rows(uow, 'usage_budget_policies', team_id, run_id)
        return rows[0] if rows else None

    async def reservation(self, uow, team_id, run_id, invocation):
        rows = await self._rows(uow, 'usage_reservations', team_id, run_id, invocation)
        return rows[0] if rows else None

    async def record(self, uow, team_id, run_id, invocation):
        rows = await self._rows(uow, 'usage_records', team_id, run_id, invocation)
        return rows[0] if rows else None

    async def reservations(self, uow, team_id, run_id):
        return await self._rows(uow, 'usage_reservations', team_id, run_id)

    async def records(self, uow, team_id, run_id):
        return await self._rows(uow, 'usage_records', team_id, run_id)

    async def quarantined(self, uow, team_id, run_id, invocation):
        return await self._rows(uow, 'usage_quarantine', team_id, run_id, invocation)

    async def insert(self, uow, resource, values):
        await uow.execute(insert(TABLES[resource]).values(**values))

    async def change(self, uow, resource, team_id, run_id, invocation, values):
        table = TABLES[resource]
        await uow.execute(
            update(table).where(*self.scope(table, team_id, run_id, invocation)).values(**values)
        )

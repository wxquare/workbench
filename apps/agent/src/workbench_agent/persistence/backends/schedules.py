"""Durable schedule operations; callers supply the write UoW."""

import json
from sqlalchemy import and_, or_, insert, select, update
from workbench_agent.persistence import schema
from workbench_agent.domain.guards import TERMINAL_RUN_STATUSES


class SQLiteScheduleStore:
    async def one(self, uow, team_id, schedule_id):
        t = schema.schedules
        return (
            (
                await uow.execute(
                    select(t).where(t.c.team_id == team_id, t.c.schedule_id == schedule_id)
                )
            )
            .mappings()
            .first()
        )

    async def list(self, uow, team_id, agent_id, limit, after=None):
        t = schema.schedules
        statement = select(t).where(t.c.team_id == team_id, t.c.agent_id == agent_id)
        if after:
            statement = statement.where(t.c.schedule_id > after)
        return list(
            (await uow.execute(statement.order_by(t.c.schedule_id).limit(limit))).mappings()
        )

    async def insert(self, uow, values):
        await uow.execute(insert(schema.schedules).values(**values))

    async def due(self, uow, now, limit=100, after=None):
        t = schema.schedules
        statement = select(t).where(t.c.status == 'active', t.c.next_due_at <= now)
        if after is not None:
            statement = statement.where(
                or_(
                    t.c.next_due_at > after[0],
                    and_(t.c.next_due_at == after[0], t.c.schedule_id > after[1]),
                )
            )
        return list(
            (
                await uow.execute(statement.order_by(t.c.next_due_at, t.c.schedule_id).limit(limit))
            ).mappings()
        )

    async def advance(self, uow, team_id, schedule_id, revision, values):
        t = schema.schedules
        result = await uow.execute(
            update(t)
            .where(t.c.team_id == team_id, t.c.schedule_id == schedule_id, t.c.revision == revision)
            .values(**values)
        )
        return result.rowcount == 1

    async def fire(self, uow, values):
        await uow.execute(insert(schema.schedule_fires).values(**values))

    async def manual_fire(self, uow, values):
        await uow.execute(insert(schema.schedule_manual_fires).values(**values))

    async def history_cursor_exists(self, uow, team_id, schedule_id, cursor):
        table = (
            schema.schedule_manual_fires if cursor['kind'] == 'manual' else schema.schedule_fires
        )
        statement = (
            select(table.c.fire_id)
            .where(
                table.c.team_id == team_id,
                table.c.schedule_id == schedule_id,
                table.c.fire_id == cursor['fire_id'],
                table.c.created_at == cursor['created_at'],
            )
            .limit(1)
        )
        return (await uow.execute(statement)).first() is not None

    async def history(self, uow, team_id, schedule_id, limit=100, after=None):
        values = []
        for kind, table in (
            ('scheduled', schema.schedule_fires),
            ('manual', schema.schedule_manual_fires),
        ):
            statement = select(table).where(
                table.c.team_id == team_id, table.c.schedule_id == schedule_id
            )
            if after is not None:
                tied = table.c.fire_id < after['fire_id']
                if kind < after['kind']:
                    tied = or_(tied, table.c.fire_id == after['fire_id'])
                statement = statement.where(
                    or_(
                        table.c.created_at < after['created_at'],
                        and_(table.c.created_at == after['created_at'], tied),
                    )
                )
            rows = (
                await uow.execute(
                    statement.order_by(table.c.created_at.desc(), table.c.fire_id.desc()).limit(
                        limit
                    )
                )
            ).mappings()
            for row in rows:
                values.append(
                    {
                        'fire_id': row['fire_id'],
                        'kind': kind,
                        'due_at': row['due_at'] if kind == 'scheduled' else None,
                        'run_id': row['run_id'],
                        'status': row['status'] if kind == 'scheduled' else 'started',
                        'created_at': row['created_at'],
                    }
                )
        return sorted(
            values, key=lambda row: (row['created_at'], row['fire_id'], row['kind']), reverse=True
        )[:limit]

    async def active_run(self, uow, team_id, schedule_id):
        f, r = schema.schedule_fires, schema.runs
        statement = (
            select(r.c.run_id)
            .select_from(f.join(r, (f.c.team_id == r.c.team_id) & (f.c.run_id == r.c.run_id)))
            .where(
                f.c.team_id == team_id,
                f.c.schedule_id == schedule_id,
                r.c.status.not_in(TERMINAL_RUN_STATUSES),
            )
            .limit(1)
        )
        if (await uow.execute(statement)).first() is not None:
            return True
        m = schema.schedule_manual_fires
        statement = (
            select(r.c.run_id)
            .select_from(m.join(r, (m.c.team_id == r.c.team_id) & (m.c.run_id == r.c.run_id)))
            .where(
                m.c.team_id == team_id,
                m.c.schedule_id == schedule_id,
                r.c.status.not_in(TERMINAL_RUN_STATUSES),
            )
            .limit(1)
        )
        if (await uow.execute(statement)).first() is not None:
            return True
        schedule = await self.one(uow, team_id, schedule_id)
        manual = json.loads(schedule['payload']).get('last_manual_run_id') if schedule else None
        if not manual:
            return False
        return (
            await uow.execute(
                select(r.c.run_id)
                .where(
                    r.c.team_id == team_id,
                    r.c.run_id == manual,
                    r.c.status.not_in(TERMINAL_RUN_STATUSES),
                )
                .limit(1)
            )
        ).first() is not None

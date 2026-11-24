"""Fenced, independent maintenance claims for persisted Runner resources."""

from uuid import uuid4
from sqlalchemy import and_, exists, insert, or_, select, update
from workbench_agent.persistence import schema as s
from workbench_agent.persistence.errors import StaleExecution


class SQLiteRunnerCleanupStore:
    def __init__(self, clock):
        self.clock = clock

    async def insert(self, uow, values):
        await uow.execute(insert(s.runner_cleanup_obligations).values(**values))

    async def get(self, uow, team, invocation):
        t = s.runner_cleanup_obligations
        return (
            (
                await uow.execute(
                    select(t).where(t.c.team_id == team, t.c.invocation_id == invocation)
                )
            )
            .mappings()
            .first()
        )

    async def mark_active_clean(self, uow, team, invocation, now):
        t = s.runner_cleanup_obligations
        result = await uow.execute(
            update(t)
            .where(t.c.team_id == team, t.c.invocation_id == invocation, t.c.status == 'pending')
            .values(status='cleaned', updated_at=now)
        )
        return result.rowcount == 1

    async def advance(self, uow, team, invocation, previous, current, now):
        t = s.runner_cleanup_obligations
        result = await uow.execute(
            update(t)
            .where(
                t.c.team_id == team,
                t.c.invocation_id == invocation,
                t.c.status == 'pending',
                t.c.creation_steps == previous,
            )
            .values(creation_steps=current, updated_at=now)
        )
        return result.rowcount == 1

    @staticmethod
    def _eligible(t, now):
        lease = s.execution_leases
        active = exists(
            select(lease.c.thread_id).where(
                lease.c.team_id == t.c.team_id,
                lease.c.thread_id == t.c.thread_id,
                lease.c.owner_run_id == t.c.run_id,
                lease.c.owner_job_id == t.c.job_id,
                lease.c.worker_id == t.c.worker_id,
                lease.c.run_epoch == t.c.run_epoch,
                lease.c.expires_at > now,
            )
        )
        return and_(
            or_(
                and_(t.c.status == 'pending', t.c.next_retry_at <= now),
                and_(t.c.status == 'cleaning', t.c.cleanup_until <= now),
            ),
            ~active,
        )

    async def claim(self, uow, daemon_id):
        t = s.runner_cleanup_obligations
        now = await self.clock.now_ms(uow)
        eligible = and_(self._eligible(t, now), t.c.daemon_id == daemon_id)
        row = (
            (
                await uow.execute(
                    select(t).where(eligible).order_by(t.c.created_at, t.c.invocation_id).limit(1)
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        owner = str(uuid4())
        result = await uow.execute(
            update(t)
            .where(t.c.invocation_id == row['invocation_id'], eligible)
            .values(
                status='cleaning',
                cleanup_owner=owner,
                cleanup_until=now + 90000,
                attempts=t.c.attempts + 1,
                updated_at=now,
            )
        )
        if result.rowcount != 1:
            raise StaleExecution()
        return dict(row, cleanup_owner=owner)

    async def finish(self, uow, claim, *, success):
        t = s.runner_cleanup_obligations
        now = await self.clock.now_ms(uow)
        result = await uow.execute(
            update(t)
            .where(
                t.c.invocation_id == claim['invocation_id'],
                t.c.team_id == claim['team_id'],
                t.c.status == 'cleaning',
                t.c.cleanup_owner == claim['cleanup_owner'],
                t.c.cleanup_until > now,
            )
            .values(
                status='cleaned' if success else 'pending',
                cleanup_owner=None,
                cleanup_until=None,
                next_retry_at=0
                if success
                else now + min(300000, 60000 * (2 ** min(claim['attempts'], 3))),
                updated_at=now,
            )
        )
        if result.rowcount != 1:
            raise StaleExecution()

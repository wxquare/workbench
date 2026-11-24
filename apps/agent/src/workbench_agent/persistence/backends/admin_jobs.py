"""Admin job CAS adapter. SQL remains behind the management Store port."""

import uuid
from sqlalchemy import select, insert, update, and_, or_
from workbench_agent.domain.admin_jobs import AdminJob, ClaimedAdminJob
from workbench_agent.persistence.schema import admin_jobs as jobs, admin_job_events as events
from workbench_agent.persistence.errors import StorageError, StaleExecution


class SQLiteAdminJobStore:
    def __init__(self, clock):
        self.clock = clock

    @staticmethod
    def _view(row):
        return AdminJob(
            **{
                key: row[key]
                for key in ('job_id', 'team_id', 'agent_id', 'kind', 'status', 'result_ref')
            }
        )

    async def submit(self, uow, auth, kind, payload, request_hash, key):
        scope = and_(
            jobs.c.team_id == auth.team_id,
            jobs.c.agent_id == auth.agent_id,
            jobs.c.subject == auth.subject,
            jobs.c.kind == kind,
            jobs.c.idempotency_key == key,
        )
        previous = (await uow.execute(select(jobs).where(scope))).mappings().first()
        if previous:
            if previous['request_hash'] != request_hash:
                raise StorageError('UNIQUE_CONFLICT')
            return self._view(previous)
        now = await self.clock.now_ms(uow)
        job_id = str(uuid.uuid4())
        await uow.execute(
            insert(jobs).values(
                job_id=job_id,
                team_id=auth.team_id,
                agent_id=auth.agent_id,
                subject=auth.subject,
                kind=kind,
                status='queued',
                payload=payload,
                request_hash=request_hash,
                idempotency_key=key,
                idempotency_expires_at=now + 72 * 3600 * 1000,
                available_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        await self._event(uow, auth.team_id, job_id, 'submitted', now)
        return AdminJob(job_id, auth.team_id, auth.agent_id, kind, 'queued')

    async def get(self, uow, auth, job_id):
        row = (
            (
                await uow.execute(
                    select(jobs).where(
                        jobs.c.team_id == auth.team_id,
                        jobs.c.agent_id == auth.agent_id,
                        jobs.c.job_id == job_id,
                    )
                )
            )
            .mappings()
            .first()
        )
        return self._view(row) if row else None

    async def claim(self, uow, capabilities, worker_id):
        if not capabilities:
            return None
        now = await self.clock.now_ms(uow)
        eligible = and_(
            jobs.c.kind.in_(sorted(capabilities)),
            jobs.c.available_at <= now,
            or_(
                jobs.c.status == 'queued',
                and_(jobs.c.status == 'running', jobs.c.lease_until <= now),
            ),
        )
        row = (
            (
                await uow.execute(
                    select(jobs).where(eligible).order_by(jobs.c.created_at, jobs.c.job_id).limit(1)
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        revision = row['revision'] + 1
        changed = await uow.execute(
            update(jobs)
            .where(jobs.c.job_id == row['job_id'], jobs.c.revision == row['revision'], eligible)
            .values(
                status='running',
                lease_owner=worker_id,
                lease_until=now + 45000,
                attempt=row['attempt'] + 1,
                revision=revision,
                updated_at=now,
            )
        )
        if changed.rowcount != 1:
            raise StaleExecution()
        await self._event(uow, row['team_id'], row['job_id'], 'claimed', now)
        return ClaimedAdminJob(
            row['job_id'],
            row['team_id'],
            row['agent_id'],
            row['kind'],
            row['payload'],
            worker_id,
            revision,
            row['attempt'] + 1,
            now + 45000,
        )

    @staticmethod
    def _owner(claim, now):
        return and_(
            jobs.c.team_id == claim.team_id,
            jobs.c.agent_id == claim.agent_id,
            jobs.c.job_id == claim.job_id,
            jobs.c.status == 'running',
            jobs.c.lease_owner == claim.worker_id,
            jobs.c.revision == claim.revision,
            jobs.c.attempt == claim.attempt,
            jobs.c.lease_until > now,
        )

    async def guard(self, uow, claim):
        now = await self.clock.now_ms(uow)
        changed = await uow.execute(
            update(jobs)
            .where(self._owner(claim, now))
            .values(guard_revision=jobs.c.guard_revision + 1)
        )
        if changed.rowcount != 1:
            raise StaleExecution()

    async def renew(self, uow, claim):
        now = await self.clock.now_ms(uow)
        await self.guard(uow, claim)
        changed = await uow.execute(
            update(jobs)
            .where(self._owner(claim, now))
            .values(lease_until=now + 45000, updated_at=now)
        )
        if changed.rowcount != 1:
            raise StaleExecution()

    async def finish(self, uow, claim, result_ref):
        await self.guard(uow, claim)
        now = await self.clock.now_ms(uow)
        changed = await uow.execute(
            update(jobs)
            .where(self._owner(claim, now))
            .values(
                status='succeeded',
                result_ref=result_ref,
                lease_owner=None,
                lease_until=None,
                revision=jobs.c.revision + 1,
                updated_at=now,
            )
        )
        if changed.rowcount != 1:
            raise StaleExecution()
        await self._event(uow, claim.team_id, claim.job_id, 'succeeded', now)

    async def upload_stage_references(self, uow):
        rows = (
            await uow.execute(
                select(jobs.c.payload).where(
                    jobs.c.kind == 'artifact_upload',
                    jobs.c.status.not_in(('succeeded', 'failed', 'cancelled')),
                )
            )
        ).scalars()
        import json, re

        values = set()
        for value in rows:
            try:
                stage = json.loads(value)['stage']
            except (ValueError, KeyError, TypeError) as error:
                raise StorageError('UPLOAD_STAGING_CORRUPT') from error
            if not isinstance(stage, str) or not re.fullmatch(r'[0-9a-f]{32}', stage):
                raise StorageError('UPLOAD_STAGING_CORRUPT')
            values.add(stage)
        return values

    async def fail(self, uow, claim):
        await self.guard(uow, claim)
        now = await self.clock.now_ms(uow)
        changed = await uow.execute(
            update(jobs)
            .where(self._owner(claim, now))
            .values(
                status='failed',
                lease_owner=None,
                lease_until=None,
                revision=jobs.c.revision + 1,
                updated_at=now,
            )
        )
        if changed.rowcount != 1:
            raise StaleExecution()
        await self._event(uow, claim.team_id, claim.job_id, 'failed', now)

    async def _event(self, uow, team_id, job_id, kind, now):
        row = (
            await uow.execute(
                select(jobs.c.last_event_seq).where(
                    jobs.c.team_id == team_id, jobs.c.job_id == job_id
                )
            )
        ).first()
        seq = row[0] + 1
        changed = await uow.execute(
            update(jobs)
            .where(
                jobs.c.team_id == team_id, jobs.c.job_id == job_id, jobs.c.last_event_seq == seq - 1
            )
            .values(last_event_seq=seq)
        )
        if changed.rowcount != 1:
            raise StaleExecution()
        await uow.execute(
            insert(events).values(
                team_id=team_id, job_id=job_id, seq=seq, type=kind, payload='{}', occurred_at=now
            )
        )

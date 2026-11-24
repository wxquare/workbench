"""Authorized creation and management of durable inspection schedules."""

from workbench_agent.runtime.graphs.ids import GraphId
import json
from datetime import datetime, timezone
from uuid import uuid4
from workbench_agent.application.authorization import Authorization
from workbench_agent.application.errors import DomainError, not_found
from workbench_agent.application.idempotency import Idempotency, canonical, digest
from workbench_agent.application.profiles import ProfileService
from workbench_agent.domain.schedules import ScheduleSpec, next_due


class ScheduleService:
    def __init__(self, backend):
        self.backend = backend
        self.authorization = Authorization(backend)
        self.idempotency = Idempotency(backend)

    async def create(self, auth, agent_id, spec: ScheduleSpec, *, key, receipt=False):
        async with self.backend.create_uow().begin_write() as uow:
            await self.authorization.current(uow, auth, agent_id, 'maintainer')

            async def operation():
                profile = await ProfileService(self.backend).published(uow, auth, agent_id)
                if (
                    profile.graph_id != GraphId.SOP_CONDITIONAL
                    or 'inspection' not in profile.allowed_modes
                ):
                    raise DomainError('WORKFLOW_UNAVAILABLE', 409)
                row = await self.backend.configuration.one(
                    uow,
                    'workflow_versions',
                    team_id=auth.team_id,
                    agent_id=agent_id,
                    workflow_version_id=str(spec.workflow_version_id),
                )
                if not row or digest(json.loads(row['definition_json'])) != row['hash']:
                    raise not_found()
                now = await self.backend.clock.now_ms(uow)
                due = int(
                    next_due(
                        spec.cron, spec.timezone, datetime.fromtimestamp(now / 1000, timezone.utc)
                    ).timestamp()
                    * 1000
                )
                schedule_id = str(uuid4())
                payload = {
                    'owner': auth.model_dump(mode='json'),
                    'spec': spec.model_dump(mode='json'),
                    'workflow_hash': row['hash'],
                    'workflow_version': row['version'],
                }
                await self.backend.schedules.insert(
                    uow,
                    dict(
                        team_id=auth.team_id,
                        schedule_id=schedule_id,
                        agent_id=agent_id,
                        cron=spec.cron,
                        timezone=spec.timezone,
                        overlap_policy='forbid',
                        next_due_at=due,
                        status='active',
                        payload=canonical(payload),
                        revision=0,
                        created_at=now,
                        updated_at=now,
                    ),
                )
                return {
                    'schedule_id': schedule_id,
                    'agent_id': agent_id,
                    'revision': 0,
                    'status': 'active',
                    'next_due_at': due,
                }

            result = await self.idempotency.execute(
                uow,
                auth,
                agent_id,
                'schedule.create',
                key,
                spec.model_dump(mode='json'),
                operation,
                status=201,
            )
        return result if receipt else result.body

    async def set_status(
        self, auth, agent_id, schedule_id, status, expected_revision, *, key, receipt=False
    ):
        if (
            status not in ('active', 'paused')
            or type(expected_revision) is not int
            or expected_revision < 0
        ):
            raise DomainError('INVALID_REQUEST', 422)
        async with self.backend.create_uow().begin_write() as uow:
            await self.authorization.current(uow, auth, agent_id, 'maintainer')

            async def operation():
                row = await self.backend.schedules.one(uow, auth.team_id, schedule_id)
                if not row or row['agent_id'] != agent_id:
                    raise not_found()
                if row['revision'] != expected_revision:
                    raise DomainError('REVISION_CONFLICT', 409)
                now = await self.backend.clock.now_ms(uow)
                revision = row['revision'] + 1
                if not await self.backend.schedules.advance(
                    uow,
                    auth.team_id,
                    schedule_id,
                    row['revision'],
                    {'status': status, 'revision': revision, 'updated_at': now},
                ):
                    raise DomainError('REVISION_CONFLICT', 409)
                return {
                    'schedule_id': schedule_id,
                    'agent_id': agent_id,
                    'revision': revision,
                    'status': status,
                    'next_due_at': row['next_due_at'],
                }

            response = await self.idempotency.execute(
                uow,
                auth,
                agent_id,
                'schedule.status',
                key,
                {
                    'schedule_id': schedule_id,
                    'status': status,
                    'expected_revision': expected_revision,
                },
                operation,
            )
        return response if receipt else response.body

    async def trigger(self, auth, agent_id, schedule_id, *, key, receipt=False):
        from workbench_agent.application.runs import RunService
        from workbench_agent.domain.runs import CreateRun

        async with self.backend.create_uow().begin_write() as uow:
            await self.authorization.current(uow, auth, agent_id, 'operator')

            async def operation():
                row = await self.backend.schedules.one(uow, auth.team_id, schedule_id)
                if not row or row['agent_id'] != agent_id:
                    raise not_found()
                if await self.backend.schedules.active_run(uow, auth.team_id, schedule_id):
                    raise DomainError('SCHEDULE_OVERLAP', 409)
                payload = json.loads(row['payload'])
                spec = ScheduleSpec.model_validate(payload['spec'])
                wf = await self.backend.configuration.one(
                    uow,
                    'workflow_versions',
                    team_id=auth.team_id,
                    agent_id=agent_id,
                    workflow_version_id=str(spec.workflow_version_id),
                )
                if (
                    not wf
                    or wf['hash'] != payload['workflow_hash']
                    or wf['version'] != payload['workflow_version']
                ):
                    raise DomainError('WORKFLOW_UNAVAILABLE', 409)
                request = CreateRun.model_validate(
                    {
                        'agent_id': agent_id,
                        'mode': 'inspection',
                        'workflow_version_id': str(spec.workflow_version_id),
                        'input': {'text': spec.input_text},
                    }
                )
                run_key = digest({'schedule_id': schedule_id, 'key': key, 'route': 'manual'})
                accepted = await RunService(self.backend).create(auth, request, run_key, _uow=uow)
                now = await self.backend.clock.now_ms(uow)
                if spec.query_scope is not None:
                    from workbench_agent.application.context import ContextResolver

                    await ContextResolver(self.backend).query_scope(
                        uow, auth, accepted.run_id, spec.query_scope.values(now)
                    )
                await self.backend.schedules.manual_fire(
                    uow,
                    dict(
                        team_id=auth.team_id,
                        fire_id=str(uuid4()),
                        schedule_id=schedule_id,
                        run_id=accepted.run_id,
                        actor=auth.subject,
                        created_at=now,
                    ),
                )
                payload['last_manual_run_id'] = accepted.run_id
                if not await self.backend.schedules.advance(
                    uow,
                    auth.team_id,
                    schedule_id,
                    row['revision'],
                    {
                        'payload': canonical(payload),
                        'revision': row['revision'] + 1,
                        'updated_at': now,
                    },
                ):
                    raise DomainError('REVISION_CONFLICT', 409)
                return accepted.model_dump(mode='json')

            response = await self.idempotency.execute(
                uow,
                auth,
                agent_id,
                'schedule.trigger',
                key,
                {'schedule_id': schedule_id},
                operation,
                status=202,
            )
        return response if receipt else response.body

    async def list(self, auth, agent_id, limit=20, after=None):
        async with self.backend.create_uow().begin_read() as uow:
            await self.authorization.current(uow, auth, agent_id)
            rows = await self.backend.schedules.list(uow, auth.team_id, agent_id, limit, after)
            items = []
            for row in rows:
                spec = json.loads(row['payload'])['spec']
                items.append(
                    {
                        'schedule_id': row['schedule_id'],
                        'agent_id': agent_id,
                        'revision': row['revision'],
                        'status': row['status'],
                        'next_due_at': row['next_due_at'],
                        'cron': row['cron'],
                        'timezone': row['timezone'],
                        'workflow_version_id': spec['workflow_version_id'],
                    }
                )
            return {
                'items': items,
                'next_cursor': items[-1]['schedule_id'] if len(items) == limit else None,
            }

    async def fires(self, auth, agent_id, schedule_id, limit=100, after=None):
        import base64
        from uuid import UUID

        if type(limit) is not int or not 1 <= limit <= 100:
            raise DomainError('INVALID_REQUEST', 422)
        async with self.backend.create_uow().begin_read() as uow:
            await self.authorization.current(uow, auth, agent_id)
            row = await self.backend.schedules.one(uow, auth.team_id, schedule_id)
            if not row or row['agent_id'] != agent_id:
                raise not_found()
            cursor = None
            if after is not None:
                try:
                    if not isinstance(after, str) or len(after) > 1024:
                        raise ValueError()
                    raw = base64.b64decode(
                        after + '=' * ((-len(after)) % 4), altchars=b'-_', validate=True
                    )
                    cursor = json.loads(raw)
                    if (
                        not isinstance(cursor, dict)
                        or set(cursor) != {'v', 'team', 'schedule', 'created_at', 'fire_id', 'kind'}
                        or type(cursor['v']) is not int
                        or cursor['v'] != 1
                        or cursor['team'] != auth.team_id
                        or cursor['schedule'] != schedule_id
                        or type(cursor['created_at']) is not int
                        or cursor['created_at'] < 0
                        or cursor['kind'] not in ('manual', 'scheduled')
                        or not isinstance(cursor['fire_id'], str)
                        or str(UUID(cursor['fire_id'])) != cursor['fire_id']
                    ):
                        raise ValueError()
                    if not await self.backend.schedules.history_cursor_exists(
                        uow, auth.team_id, schedule_id, cursor
                    ):
                        raise ValueError()
                except (ValueError, TypeError, KeyError, UnicodeError):
                    raise DomainError('INVALID_CURSOR', 422) from None
            rows = await self.backend.schedules.history(
                uow, auth.team_id, schedule_id, limit + 1, cursor
            )
            has_more = len(rows) > limit
            items = rows[:limit]
            next_cursor = None
            if has_more:
                last = items[-1]
                value = {
                    'v': 1,
                    'team': auth.team_id,
                    'schedule': schedule_id,
                    'created_at': last['created_at'],
                    'fire_id': last['fire_id'],
                    'kind': last['kind'],
                }
                next_cursor = (
                    base64.urlsafe_b64encode(canonical(value).encode()).decode().rstrip('=')
                )
            return {'items': items, 'next_cursor': next_cursor}

"""Authorized public event operations. Every returned batch has closed its UoW."""

import asyncio
import json
import time
from typing import get_args
from uuid import UUID, uuid5
from workbench_agent.domain.events import RunEvent, EventType
from workbench_agent.domain.context import RunContext
from workbench_agent.domain.guards import TERMINAL_RUN_STATUSES
from workbench_agent.domain.runs import timestamp
from workbench_agent.application.authorization import Authorization
from workbench_agent.application.context import ContextResolver
from workbench_agent.application.errors import DomainError, not_found

RETENTION_MS = 7 * 24 * 3600 * 1000


class CursorExpired(DomainError):
    def __init__(self, run_id):
        super().__init__('EVENT_CURSOR_EXPIRED', 410)
        self.links = {'result': f'/v1/runs/{run_id}'}


def envelope(row):
    body = json.loads(row['payload'])
    event_type = row['type']
    # T07 persisted a private waiting alias; versioned wire projection leaves history intact.
    if event_type == 'run.waiting':
        event_type = (
            'approval.required' if body.get('name') == 'foundation.approval' else 'input.required'
        )
    elif event_type == 'run.cancel_requested':
        event_type = 'run.status_changed'
        body = dict(body, status='cancel_requested')
    elif event_type == 'run.resume_accepted':
        event_type = 'run.resumed'
    return RunEvent(
        schema_version=row['schema_version'],
        run_id=row['run_id'],
        seq=row['seq'],
        type=event_type,
        occurred_at=timestamp(row['occurred_at']),
        attempt=row['attempt'],
        message_id=row['message_id'],
        payload=body,
    )


class RunEventStore:
    def __init__(self, backend, hints=None):
        self.backend, self.hints = backend, hints

    async def _authorized(self, uow, auth, run_id, minimum='viewer'):
        run = await self.backend.runs.one(uow, 'runs', team_id=auth.team_id, run_id=run_id)
        if run is None:
            raise not_found()
        await Authorization(self.backend).current(uow, auth, run['agent_id'], minimum)
        return run

    async def _cursor(self, uow, auth, run_id, after_seq):
        run = await self._authorized(uow, auth, run_id)
        if type(after_seq) is not int or after_seq < 0 or after_seq > run['last_event_seq']:
            raise DomainError('INVALID_EVENT_CURSOR', 422)
        now = await self.backend.clock.now_ms(uow)
        floor = max(
            run['event_floor'],
            await self.backend.execution_events.expired_floor(
                uow, auth.team_id, run_id, now - RETENTION_MS
            ),
        )
        if floor and after_seq <= floor:
            raise CursorExpired(run_id)
        return run

    async def stream_state(self, auth, run_id, after_seq):
        async with self.backend.create_uow().begin_read() as uow:
            return dict(await self._cursor(uow, auth, run_id, after_seq))

    async def read_batch(self, auth, run_id, after_seq, limit=100):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise DomainError('INVALID_EVENT_LIMIT', 422)
        async with self.backend.create_uow().begin_read() as uow:
            run = await self._cursor(uow, auth, run_id, after_seq)
            rows = await self.backend.execution_events.rows(
                uow, auth.team_id, run_id, after_seq, limit
            )
            permitted = await self._may_disclose(uow, auth, run)
            events = [self._project(envelope(row), run, permitted) for row in rows]
        return events

    async def _may_disclose(self, uow, auth, run):
        from workbench_agent.application.disclosure import readable_evidence

        rows = await self.backend.runs.all_evidence(uow, auth.team_id, run['run_id'])
        refs = {row['evidence_id'] for row in rows}
        return (
            bool(refs) and set(await readable_evidence(self.backend, uow, run, auth, refs)) == refs
        )

    @staticmethod
    def _project(event, run, permitted):
        context = RunContext.model_validate_json(run['versions'])
        if permitted:
            return event
        if event.type == 'assistant.delta':
            return event.model_copy(update={'payload': {'text': '[Content unavailable]'}})
        if event.type in ('tool.started', 'tool.completed', 'evidence.added'):
            return event.model_copy(update={'payload': {'redacted': True}})
        if event.type == 'run.completed':
            return event.model_copy(
                update={
                    'payload': {
                        'terminal_status': event.payload.get('terminal_status')
                        if event.payload.get('terminal_status')
                        in ('succeeded', 'partial', 'failed', 'cancelled', 'expired', 'timed_out')
                        else 'partial',
                        'summary': 'Analysis details are unavailable under your current evidence access.',
                        'links': {'result': f'/v1/runs/{event.run_id}'},
                    }
                }
            )
        return event

    async def project_event(self, auth, event):
        # Recheck after queuing and immediately before sending on an SSE connection.
        async with self.backend.create_uow().begin_read() as uow:
            run = await self._authorized(uow, auth, event.run_id)
            permitted = await self._may_disclose(uow, auth, run)
            return self._project(event, run, permitted)

    async def is_terminal_and_drained(self, auth, run_id, after_seq):
        async with self.backend.create_uow().begin_read() as uow:
            run = await self._cursor(uow, auth, run_id, after_seq)
            return run['status'] in TERMINAL_RUN_STATUSES and run['last_event_seq'] <= after_seq

    async def wait_for_hint_or_timeout(self, run_id, seconds):
        seconds = max(0, min(seconds, 1))
        deadline = time.monotonic() + seconds
        try:
            if self.hints:
                async with asyncio.timeout(seconds):
                    await self.hints.wait(run_id, seconds)
                return
        except Exception:
            pass  # Hint failure never determines durable state or leaks endpoint secrets.
        await asyncio.sleep(max(0, deadline - time.monotonic()))

    async def _hint(self, run_id):
        if self.hints:
            try:
                async with asyncio.timeout(0.1):
                    await self.hints.publish(run_id)
            except Exception:
                pass

    async def append_execution(self, guard, type, payload):
        if type not in get_args(EventType) or type in {'action.updated', 'delivery.updated'}:
            raise ValueError('unsupported execution event')
        async with self.backend.create_uow().begin_write() as uow:
            await self.backend.lease_guard.require(uow, guard)
            run = await self.backend.runs.one(
                uow, 'runs', team_id=guard.team_id, run_id=guard.run_id
            )
            await ContextResolver(self.backend).revalidate(
                RunContext.model_validate_json(run['versions']), uow=uow
            )
            body = dict(payload)
            message_id = None
            if type == 'assistant.delta':
                attempt = await self.backend.jobs.execution_attempt(
                    uow, guard.team_id, guard.run_id
                )
                message_id = str(uuid5(UUID(guard.run_id), f'draft:{attempt}'))
                if not isinstance(body.get('text'), str) or not body['text']:
                    raise ValueError('delta text required')
                body.update(message_id=message_id, attempt=attempt)
            elif type == 'assistant.message_superseded':
                message_id = body.get('message_id')
            seq = await self.backend.execution_events.append_execution(
                uow, guard, type, body, message_id=message_id
            )
            value = envelope(
                (
                    await self.backend.execution_events.rows(
                        uow, guard.team_id, guard.run_id, seq - 1, 1
                    )
                )[0]
            )
        await self._hint(guard.run_id)
        return value

    async def append_control(self, auth, run_id, type, payload):
        # Lifecycle transitions themselves remain in their same-UoW services.
        states = {
            'run.status_changed': {'cancel_requested'},
            'run.resumed': {'queued'},
            'input.required': {'waiting_input'},
            'approval.required': {'waiting_approval'},
        }
        if type not in states:
            raise ValueError('unsupported authorized control event')
        async with self.backend.create_uow().begin_write() as uow:
            run = await self._authorized(uow, auth, run_id, 'operator')
            if run['status'] not in states[type]:
                raise DomainError('EVENT_STATE_CONFLICT', 409)
            if type == 'run.status_changed':
                payload = dict(payload, status=run['status'], revision=run['revision'])
            seq = await self.backend.execution_events.append_control(
                uow, auth.team_id, run_id, type, payload
            )
            value = envelope(
                (await self.backend.execution_events.rows(uow, auth.team_id, run_id, seq - 1, 1))[0]
            )
        await self._hint(run_id)
        return value

    async def append_delivery(self, action_guard, type, payload):
        if type not in {'action.updated', 'delivery.updated'}:
            raise ValueError('unsupported delivery event')
        async with self.backend.create_uow().begin_write() as uow:
            await self.backend.execution_events.require_delivery(uow, action_guard)
            seq = await self.backend.execution_events.append_control(
                uow,
                action_guard.team_id,
                action_guard.run_id,
                type,
                dict(payload, action_id=action_guard.action_id, outbox_id=action_guard.outbox_id),
            )
            value = envelope(
                (
                    await self.backend.execution_events.rows(
                        uow, action_guard.team_id, action_guard.run_id, seq - 1, 1
                    )
                )[0]
            )
        await self._hint(action_guard.run_id)
        return value

    async def prune(self, auth, run_id):
        async with self.backend.create_uow().begin_write() as uow:
            await self._authorized(uow, auth, run_id, 'maintainer')
            now = await self.backend.clock.now_ms(uow)
            await self.backend.execution_events.prune(uow, auth.team_id, run_id, now - RETENTION_MS)

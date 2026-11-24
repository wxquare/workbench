"""Authorized atomic interrupt CAS admission and replay-safe input decisions."""

from workbench_agent.runtime.graphs.ids import GraphId
import json
from uuid import uuid4
from dataclasses import dataclass
from workbench_agent.domain.runs import AcceptedRun, timestamp, run_links
from workbench_agent.application.errors import DomainError, not_found
from workbench_agent.application.idempotency import canonical, digest
from workbench_agent.runtime.graphs.interrupts import validate_response


@dataclass(frozen=True)
class ResumeDecision:
    command: dict | None
    already_applied: bool


class ResumeApplier:
    def apply(self, resume_request, checkpoint) -> ResumeDecision:
        values = checkpoint['values']
        if values.get('last_applied_resume_id') == resume_request['request_id']:
            return ResumeDecision(None, True)
        real = next(
            (i for i in checkpoint['interrupts'] if i.id == resume_request['graph_interrupt_id']),
            None,
        )
        if real is None:
            raise ValueError('accepted named interrupt not present in checkpoint')
        descriptor = json.loads(resume_request['schema'])
        response = json.loads(resume_request['response'])
        if real.value != descriptor or not validate_response(descriptor, response):
            raise ValueError('accepted interrupt response no longer matches checkpoint')
        if (
            descriptor['kind'] == 'approval'
            and values.get('args_hash') != resume_request['args_hash']
        ):
            raise ValueError('approval parameters changed')
        return ResumeDecision(
            {
                'resume_request_id': resume_request['request_id'],
                'response': json.loads(resume_request['response']),
            },
            False,
        )


class ResumeService:
    def __init__(self, runs):
        self.runs = runs
        self.backend = runs.backend
        self.store = runs.store

    async def resume(
        self,
        auth,
        run_id,
        request,
        key,
        *,
        source_connector=None,
        source_thread=None,
        source_event=None,
        source_payload=None,
    ):
        payload = request.model_dump(mode='json')
        async with self.backend.create_uow().begin_write() as uow:
            run = await self.store.one(uow, 'runs', team_id=auth.team_id, run_id=run_id)
            if run is None:
                raise not_found()
            await self.runs.authorization.current(uow, auth, run['agent_id'], 'operator')
            source = None
            if source_connector is not None:
                if (
                    auth.identity_kind != 'connector'
                    or not source_thread
                    or not source_event
                    or source_payload is None
                ):
                    raise DomainError('OPERATION_DENIED', 403)
                mapping = await self.store.one(
                    uow,
                    'source_events',
                    team_id=auth.team_id,
                    connector_id='thread:' + digest(source_connector),
                    external_event_id=source_thread,
                )
                now = await self.backend.clock.now_ms(uow)
                if not mapping or mapping['expires_at'] <= now:
                    raise DomainError('SOURCE_CONFLICT', 409)
                mapped = json.loads(mapping['response_ref'])
                if mapped != {'agent_id': run['agent_id'], 'thread_id': run['thread_id']}:
                    raise DomainError('SOURCE_CONFLICT', 409)
                interrupt = await self.store.one(
                    uow,
                    'interrupts',
                    team_id=auth.team_id,
                    run_id=run_id,
                    interrupt_id=str(request.interrupt_id),
                )
                if (
                    interrupt is None
                    or json.loads(interrupt['schema']).get('actor') != auth.subject
                ):
                    raise DomainError('INTERRUPT_ACTOR_MISMATCH', 403)
                namespace = 'connector:' + digest(source_connector)
                source = await self.store.one(
                    uow,
                    'source_events',
                    team_id=auth.team_id,
                    connector_id=namespace,
                    external_event_id=source_event,
                )
                if source and source['expires_at'] > now:
                    envelope = json.loads(source['response_ref'])
                    if envelope.get('scope') != {
                        'connector_id': source_connector,
                        'agent_id': run['agent_id'],
                    } or source['request_hash'] != digest(source_payload):
                        raise DomainError('SOURCE_CONFLICT', 409)
                    return AcceptedRun(**envelope['accepted'], replayed=True)

            async def operation():
                now = await self.backend.clock.now_ms(uow)
                if run['revision'] != request.expected_revision:
                    raise DomainError('REVISION_CONFLICT', 409)
                if run['status'] not in ('waiting_input', 'waiting_approval'):
                    raise DomainError('INTERRUPT_CONFLICT', 409)
                row = await self.store.one(
                    uow,
                    'interrupts',
                    team_id=auth.team_id,
                    run_id=run_id,
                    interrupt_id=str(request.interrupt_id),
                )
                if row is None or row['status'] != 'open':
                    raise DomainError('INTERRUPT_CONFLICT', 409)
                if (
                    min(row['expires_at'], run['waiting_deadline_at'], run['wall_deadline_at'])
                    <= now
                ):
                    raise DomainError('INTERRUPT_EXPIRED', 409)
                descriptor = json.loads(row['schema'])
                if descriptor['actor'] != auth.subject:
                    raise DomainError('INTERRUPT_ACTOR_MISMATCH', 403)
                response = payload['response']
                if not validate_response(descriptor, response):
                    raise DomainError('INVALID_RESPONSE', 422)
                saved = await self.store.checkpoint(uow, auth.team_id, run_id)
                real = (
                    next(
                        (i for i in saved['interrupts'] if i.id == row['graph_interrupt_id']), None
                    )
                    if saved
                    else None
                )
                if real is None or real.value != descriptor:
                    raise DomainError('INTERRUPT_CONFLICT', 409)
                if response['kind'] == 'approval':
                    if (
                        response['approval_id'] != descriptor['approval_id']
                        or response['expected_args_hash'] != row['args_hash']
                        or saved['values'].get('args_hash') != row['args_hash']
                    ):
                        raise DomainError('APPROVAL_INVALIDATED', 409)
                request_id = str(uuid4())
                await self.store.insert(
                    uow,
                    'resume_requests',
                    dict(
                        team_id=auth.team_id,
                        request_id=request_id,
                        run_id=run_id,
                        interrupt_id=row['interrupt_id'],
                        response=canonical(response),
                        status='submitted',
                        idempotency_key=key,
                        created_at=now,
                        updated_at=now,
                    ),
                )
                changed = await self.store.change(
                    uow,
                    'interrupts',
                    {
                        'interrupt_id': row['interrupt_id'],
                        'status': 'open',
                        'accepted_resume_request_id': None,
                    },
                    {
                        'accepted_resume_request_id': request_id,
                        'status': 'response_submitted',
                        'revision': row['revision'] + 1,
                        'updated_at': now,
                    },
                )
                if changed != 1:
                    raise DomainError('INTERRUPT_CONFLICT', 409)
                if response['kind'] == 'user_input':
                    if json.loads(run['versions'])['graph_id'] in (
                        GraphId.RCA_SYNTHETIC,
                        GraphId.RCA_PROPOSAL,
                        GraphId.RCA_BATCH_PROPOSAL,
                    ):
                        current = await self.store.current_scope(uow, auth.team_id, run_id)
                        if (
                            not current
                            or not descriptor.get('expected_scope_id')
                            or current['scope_id'] != descriptor['expected_scope_id']
                        ):
                            raise DomainError('STALE_QUERY_SCOPE', 409)
                        scope_values = json.loads(current['scope_json'])
                        if not isinstance(scope_values, dict) or 'region' in scope_values:
                            raise DomainError('INVALID_SCOPE', 422)
                        scope_values['region'] = response['values']['region']
                        await self.runs.contexts.query_scope(uow, auth, run_id, scope_values)
                    else:
                        await self.runs.contexts.query_scope(uow, auth, run_id, response['values'])
                await self.store.insert(
                    uow,
                    'messages',
                    dict(
                        team_id=auth.team_id,
                        message_id=str(uuid4()),
                        thread_id=run['thread_id'],
                        run_id=run_id,
                        role='user',
                        content_ref=canonical(
                            {
                                'input': {
                                    'type': 'text',
                                    'text': response.get('text') or canonical(response),
                                    'artifact_ids': [],
                                },
                                'resume_request_id': request_id,
                            }
                        ),
                        created_at=now,
                        updated_at=now,
                    ),
                )
                generation = await self.store.generation(uow, run_id) + 1
                await self.store.insert(
                    uow,
                    'run_jobs',
                    dict(
                        team_id=auth.team_id,
                        job_id=str(uuid4()),
                        run_id=run_id,
                        generation=generation,
                        kind='resume',
                        status='ready',
                        available_at=now,
                        capabilities='[]',
                        created_at=now,
                        updated_at=now,
                    ),
                )
                revision = run['revision'] + 1
                # Exact checkpoint/interrupt evidence above authorizes clearing projection intent.
                await self.store.change(
                    uow,
                    'runs',
                    {'run_id': run_id, 'revision': run['revision']},
                    dict(
                        status='queued',
                        recovery_phase=None,
                        waiting_deadline_at=None,
                        deadline_at=None,
                        revision=revision,
                        updated_at=now,
                    ),
                )
                await self.backend.execution_events.append_control(
                    uow,
                    auth.team_id,
                    run_id,
                    'run.resumed',
                    {
                        'resume_request_id': request_id,
                        'generation': generation,
                        'revision': revision,
                    },
                )
                accepted = AcceptedRun(
                    run_id=run_id,
                    thread_id=run['thread_id'],
                    revision=revision,
                    accepted_at=timestamp(now),
                    links=run_links(run_id),
                ).model_dump(mode='json')
                if source_connector is not None:
                    values = dict(
                        request_hash=digest(source_payload),
                        response_ref=canonical(
                            {
                                'scope': {
                                    'connector_id': source_connector,
                                    'agent_id': run['agent_id'],
                                },
                                'accepted': accepted,
                            }
                        ),
                        expires_at=now + 90 * 24 * 3600 * 1000,
                        updated_at=now,
                    )
                    if source:
                        await self.store.replace_source(uow, source['source_event_id'], values)
                    else:
                        await self.store.insert(
                            uow,
                            'source_events',
                            dict(
                                team_id=auth.team_id,
                                source_event_id=str(uuid4()),
                                connector_id=namespace,
                                external_event_id=source_event,
                                created_at=now,
                                **values,
                            ),
                        )
                return accepted

            result = await self.runs.idempotency.execute(
                uow,
                auth,
                run['agent_id'],
                'run.resume:' + run_id,
                key,
                payload,
                operation,
                status=202,
            )
        return AcceptedRun(**result.body, replayed=result.replayed)

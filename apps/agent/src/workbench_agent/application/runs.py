"""Durable queued admission. No models, graphs, Redis, or external calls in a UoW."""

from workbench_agent.runtime.graphs.ids import GraphId
import json
from contextlib import nullcontext
from uuid import uuid4
from workbench_agent.domain.runs import (
    AcceptedRun,
    CreateRun,
    ThreadView,
    MessageView,
    RunView,
    timestamp,
    run_links,
)
from workbench_agent.domain.context import RunContext
from workbench_agent.application.authorization import Authorization
from workbench_agent.application.context import ContextResolver
from workbench_agent.application.profiles import ProfileService
from workbench_agent.application.errors import DomainError, not_found
from workbench_agent.application.idempotency import Idempotency, canonical, digest
from workbench_agent.application.summary_policy import (
    SUMMARY_INPUT_BYTE_LIMIT,
    SUMMARY_MESSAGE_LIMIT,
    choose_summary_reserve,
)
from workbench_agent.observability import observe_run_admission, current_telemetry

# wall_seconds limits active execution; runs.wall_deadline_at separately caps total lifetime at 48h.
DEFAULT_BUDGETS = {
    'auto': (180, 8, 20, 80000),
    'qa': (180, 8, 20, 80000),
    'rca': (300, 12, 30, 120000),
    'code': (600, 12, 30, 120000),
    'sop': (600, 12, 40, 120000),
    'inspection': (600, 12, 40, 120000),
}
BUDGET_FIELDS = ('wall_seconds', 'model_calls', 'tool_calls', 'tokens')

# Unforgeable through public request fields; only the signed synthetic adapter holds it.
_SYNTHETIC_ALERT_V2 = object()


def _admission_payload(request):
    payload = request.model_dump(mode='json')
    # The text cap is independent of canonical JSON escaping/envelope overhead.
    if len(request.input.text.encode('utf-8')) > 65536:
        raise DomainError('INPUT_TOO_LARGE', 413)
    metadata = {k: v for k, v in payload.items() if k != 'input'}
    if len(canonical(metadata).encode('utf-8')) > 65536:
        raise DomainError('INPUT_TOO_LARGE', 413)
    return payload


def _admission_budgets(request, profile):
    budgets = dict(zip(BUDGET_FIELDS, DEFAULT_BUDGETS[request.mode]))
    if profile.budget_limits is not None:
        for field, value in profile.budget_limits.model_dump(exclude_none=True).items():
            budgets[field] = min(budgets[field], value)
    for field, value in request.budgets.model_dump(exclude_none=True).items():
        if value > budgets[field]:
            raise DomainError('BUDGET_EXCEEDED', 422)
        budgets[field] = value
    return budgets


def _validate_conversation_request(request, profile):
    if request.business_context.get('conversation') is True and profile.graph_id not in (
        GraphId.CONVERSATION_MULTI_TURN,
        GraphId.CONVERSATION_TOOL_USE,
    ):
        raise DomainError('CONVERSATION_WORKFLOW_REQUIRED', 409)
    if (
        profile.graph_id
        in (
            GraphId.CONVERSATION_MULTI_TURN,
            GraphId.CONVERSATION_TOOL_USE,
        )
        and request.mode != 'qa'
    ):
        raise DomainError('WORKFLOW_UNAVAILABLE', 409)
    if (
        profile.graph_id == GraphId.CONVERSATION_MULTI_TURN
        and 'query_scope' in request.business_context
    ):
        raise DomainError('WORKFLOW_UNAVAILABLE', 409)
    if profile.graph_id == GraphId.CONVERSATION_TOOL_USE:
        from workbench_agent.domain.query_scope import validate_qa_scope

        try:
            validate_qa_scope(request.business_context.get('query_scope'))
        except ValueError:
            raise DomainError('INVALID_SCOPE', 422) from None
        if request.business_context.get('conversation') is not True:
            raise DomainError('CONVERSATION_WORKFLOW_REQUIRED', 409)


def _validate_graph_scope(
    request, profile, auth, ingress_capability, has_caller_uow, source_connector
):
    if profile.graph_id in (
        GraphId.RCA_SYNTHETIC,
        GraphId.RCA_PROPOSAL,
        GraphId.RCA_BATCH_PROPOSAL,
    ) and not (
        ingress_capability is _SYNTHETIC_ALERT_V2
        and has_caller_uow
        and auth.identity_kind == 'connector'
        and source_connector
        and request.mode == 'rca'
    ):
        raise DomainError('WORKFLOW_UNAVAILABLE', 409)
    query_scope = None
    if profile.graph_id == GraphId.CONVERSATION_SINGLE_TURN and (
        request.mode != 'qa' or 'query_scope' in request.business_context
    ):
        raise DomainError('WORKFLOW_UNAVAILABLE', 409)
    if profile.graph_id in (
        GraphId.KNOWLEDGE_SCOPED_EXCERPT,
        GraphId.KNOWLEDGE_CITATION_REVIEW,
        GraphId.CONVERSATION_TOOL_USE,
    ):
        if request.mode != 'qa':
            raise DomainError('WORKFLOW_UNAVAILABLE', 409)
        query_scope = request.business_context.get('query_scope')
        from workbench_agent.domain.query_scope import validate_qa_scope

        try:
            query_scope = validate_qa_scope(query_scope)
        except ValueError:
            raise DomainError('INVALID_SCOPE', 422) from None
    return query_scope


def _validate_mode_and_workflow(request, profile):
    if request.mode not in profile.allowed_modes:
        raise DomainError('MODE_NOT_ALLOWED', 422)
    if profile.graph_id == GraphId.SOP_CONDITIONAL and request.mode not in (
        'sop',
        'inspection',
    ):
        raise DomainError('WORKFLOW_UNAVAILABLE', 409)
    if request.mode in ('sop', 'inspection'):
        if profile.graph_id != GraphId.SOP_CONDITIONAL or request.workflow_version_id is None:
            raise DomainError('WORKFLOW_UNAVAILABLE', 409)
    elif request.workflow_version_id is not None:
        raise DomainError('WORKFLOW_UNAVAILABLE', 409)
    if request.input.artifact_ids:
        raise DomainError('ARTIFACT_UNAVAILABLE', 409)


class RunService:
    def __init__(self, backend, *, secret_availability=None):
        from workbench_agent.connectors.secrets import SecretReferenceAvailability

        self.secret_availability = secret_availability or SecretReferenceAvailability()
        self.backend, self.store = backend, backend.runs
        self.authorization = Authorization(backend)
        self.contexts, self.profiles = ContextResolver(backend), ProfileService(backend)
        self.idempotency = Idempotency(backend)

    async def _thread(self, uow, auth, thread_id, minimum='viewer'):
        row = await self.store.one(uow, 'threads', team_id=auth.team_id, thread_id=thread_id)
        if row is None:
            raise not_found()
        await self.authorization.current(uow, auth, row['agent_id'], minimum)
        return row

    async def _new_thread(self, uow, auth, agent_id, now):
        row = dict(
            team_id=auth.team_id,
            thread_id=str(uuid4()),
            agent_id=agent_id,
            active_run_id=None,
            revision=0,
            created_at=now,
            updated_at=now,
        )
        await self.store.insert(uow, 'threads', row)
        return row

    @staticmethod
    def _thread_view(row):
        return ThreadView(
            thread_id=row['thread_id'],
            agent_id=row['agent_id'],
            active_run_id=row['active_run_id'],
            revision=row['revision'],
            created_at=timestamp(row['created_at']),
        )

    async def create_thread(self, auth, request, key):
        agent_id = str(request.agent_id)
        async with self.backend.create_uow().begin_write() as uow:
            await self.authorization.current(uow, auth, agent_id, 'operator')

            async def operation():
                now = await self.backend.clock.now_ms(uow)
                return self._thread_view(
                    await self._new_thread(uow, auth, agent_id, now)
                ).model_dump(mode='json')

            result = await self.idempotency.execute(
                uow,
                auth,
                agent_id,
                'thread.create',
                key,
                request.model_dump(mode='json'),
                operation,
                status=201,
            )
        return result

    async def get_thread(self, auth, thread_id):
        async with self.backend.create_uow().begin_read() as uow:
            return self._thread_view(await self._thread(uow, auth, thread_id))

    async def messages(self, auth, thread_id, *, limit=20, after=None):
        if not 1 <= limit <= 100:
            raise DomainError('INVALID_REQUEST', 422)
        async with self.backend.create_uow().begin_read() as uow:
            await self._thread(uow, auth, thread_id)
            rows = await self.store.messages(uow, auth.team_id, thread_id, limit=limit, after=after)
            from workbench_agent.application.disclosure import project_outcome

            items = []
            for row in rows:
                content = json.loads(row['content_ref'])
                outcome = content.get('outcome')
                if outcome is not None and row['run_id']:
                    run = await self.store.one(
                        uow, 'runs', team_id=auth.team_id, run_id=row['run_id']
                    )
                    outcome = (
                        await project_outcome(self.backend, uow, run, auth, outcome)
                        if run
                        else None
                    )
                items.append(
                    MessageView(
                        message_id=row['message_id'],
                        thread_id=thread_id,
                        run_id=row['run_id'],
                        role=row['role'],
                        input=content.get('input'),
                        outcome=outcome,
                        created_at=timestamp(row['created_at']),
                    ).model_dump(mode='json')
                )
        return {
            'items': items,
            'next_cursor': rows[-1]['message_id'] if len(rows) == limit else None,
        }

    async def create_in(self, uow, auth, request: CreateRun, idempotency_key: str) -> AcceptedRun:
        if auth.identity_kind not in ('user', 'development'):
            raise DomainError('FORBIDDEN', 403)
        return await self.create(auth, request, idempotency_key, _uow=uow)

    @observe_run_admission
    async def create(
        self,
        auth,
        request: CreateRun,
        idempotency_key: str,
        *,
        source_connector: str | None = None,
        source_thread: str | None = None,
        _uow=None,
        _ingress_capability=None,
    ) -> AcceptedRun:
        agent_id = str(request.agent_id)
        if source_connector is not None and (
            auth.identity_kind != 'connector' or not source_connector or len(source_connector) > 128
        ):
            raise DomainError('OPERATION_DENIED', 403)
        if source_thread is not None and (
            source_connector is None
            or not source_thread
            or len(source_thread) > 128
            or request.thread_id
        ):
            raise DomainError('INVALID_REQUEST', 422)
        payload = _admission_payload(request)
        async with (
            self.backend.create_uow().begin_write() if _uow is None else nullcontext(_uow)
        ) as uow:
            await self.authorization.current(uow, auth, agent_id, 'operator')
            thread = None
            if request.thread_id:
                thread = await self._thread(uow, auth, str(request.thread_id), 'operator')
                if thread['agent_id'] != agent_id:
                    raise not_found()

            async def operation():
                nonlocal thread
                now = await self.backend.clock.now_ms(uow)
                source, namespace = None, None
                if request.client_message_id:
                    # Persist the scope as well as its namespace hash; verify both before replay.
                    scope = (
                        {'connector_id': source_connector, 'agent_id': agent_id}
                        if source_connector
                        else {
                            'subject': auth.subject,
                            'agent_id': agent_id,
                            'identity_kind': auth.identity_kind,
                        }
                    )
                    namespace = (
                        ('connector:' + digest(source_connector))
                        if source_connector
                        else ('api:' + digest(scope))
                    )
                    source = await self.store.one(
                        uow,
                        'source_events',
                        team_id=auth.team_id,
                        connector_id=namespace,
                        external_event_id=request.client_message_id,
                    )
                    if source and source['expires_at'] > now:
                        envelope = json.loads(source['response_ref'])
                        if envelope['scope'] != scope or source['request_hash'] != digest(payload):
                            raise DomainError('SOURCE_CONFLICT', 409)
                        return envelope['accepted']
                if request.expected_revision is not None and (
                    thread is None or thread['revision'] != request.expected_revision
                ):
                    raise DomainError('REVISION_CONFLICT', 409)
                profile = await self.profiles.published(uow, auth, agent_id)
                _validate_conversation_request(request, profile)
                if request.business_context.get('conversation') is True and thread is not None:
                    from sqlalchemy import case, func, select
                    from workbench_agent.persistence import schema

                    pending = (
                        await uow.execute(
                            select(schema.runs.c.run_id)
                            .where(
                                schema.runs.c.team_id == auth.team_id,
                                schema.runs.c.thread_id == thread['thread_id'],
                                schema.runs.c.status.not_in(
                                    (
                                        'succeeded',
                                        'partial',
                                        'failed',
                                        'cancelled',
                                        'timed_out',
                                        'expired',
                                    )
                                ),
                            )
                            .limit(1)
                        )
                    ).first()
                    if pending:
                        raise DomainError('THREAD_BUSY', 409)
                    graph = case(
                        (
                            func.json_valid(schema.runs.c.versions) == 1,
                            func.json_extract(schema.runs.c.versions, '$.graph_id'),
                        ),
                        else_='',
                    )
                    incompatible = (
                        await uow.execute(
                            select(schema.runs.c.run_id)
                            .where(
                                schema.runs.c.team_id == auth.team_id,
                                schema.runs.c.agent_id == agent_id,
                                schema.runs.c.thread_id == thread['thread_id'],
                                schema.runs.c.status == 'succeeded',
                                func.coalesce(graph, '').not_in(
                                    (
                                        GraphId.CONVERSATION_SINGLE_TURN,
                                        GraphId.CONVERSATION_MULTI_TURN,
                                        GraphId.CONVERSATION_TOOL_USE,
                                    )
                                ),
                            )
                            .limit(1)
                        )
                    ).first()
                    if incompatible:
                        raise DomainError('CONVERSATION_HISTORY_INCOMPATIBLE', 409)
                query_scope = _validate_graph_scope(
                    request, profile, auth, _ingress_capability, _uow is not None, source_connector
                )
                _validate_mode_and_workflow(request, profile)
                budgets = _admission_budgets(request, profile)
                context = await self.contexts.resolve(auth, agent_id, uow=uow)
                if request.workflow_version_id is not None:
                    from workbench_agent.domain.profiles import VersionRef

                    workflow = await self.backend.configuration.one(
                        uow,
                        'workflow_versions',
                        team_id=auth.team_id,
                        agent_id=agent_id,
                        workflow_version_id=str(request.workflow_version_id),
                    )
                    if (
                        not workflow
                        or digest(json.loads(workflow['definition_json'])) != workflow['hash']
                    ):
                        raise DomainError('WORKFLOW_UNAVAILABLE', 409)
                    context = context.model_copy(
                        update={
                            'workflow': VersionRef(
                                resource_id=workflow['workflow_version_id'],
                                version=str(workflow['version']),
                                content_hash=workflow['hash'],
                            )
                        }
                    )
                for ref in (
                    context.default_model,
                    *(binding.ref for binding in context.role_models),
                ):
                    model_version = await self.profiles.model_version(uow, auth, ref)
                    if not self.secret_availability.available(model_version.secret_ref):
                        raise DomainError('MODEL_PROFILE_UNAVAILABLE', 409)
                summary_model = (
                    await self.profiles.model_version(uow, auth, context.model_for('summary'))
                ).configuration
                bounded_summary_graph = profile.graph_id in (
                    GraphId.KNOWLEDGE_CITATION_REVIEW,
                    GraphId.CONVERSATION_TOOL_USE,
                )
                try:
                    summary_tokens = (
                        choose_summary_reserve(
                            context_limit=summary_model.max_context_tokens,
                            output_limit=summary_model.max_output_tokens,
                            input_byte_limit=SUMMARY_INPUT_BYTE_LIMIT,
                            message_limit=SUMMARY_MESSAGE_LIMIT,
                            explicit=request.summary_token_reserve,
                        )
                        if bounded_summary_graph
                        else (request.summary_token_reserve or summary_model.max_context_tokens)
                    )
                except ValueError:
                    raise DomainError('SUMMARY_RESERVE_INVALID', 422) from None
                if summary_tokens > summary_model.max_context_tokens:
                    raise DomainError('SUMMARY_RESERVE_INVALID', 422)
                thread_map = None
                if source_thread:
                    map_namespace = 'thread:' + digest(source_connector)
                    thread_map = await self.store.one(
                        uow,
                        'source_events',
                        team_id=auth.team_id,
                        connector_id=map_namespace,
                        external_event_id=source_thread,
                    )
                    if thread_map and thread_map['expires_at'] > now:
                        mapped = json.loads(thread_map['response_ref'])
                        if mapped['agent_id'] != agent_id:
                            raise DomainError('SOURCE_CONFLICT', 409)
                        thread = await self._thread(uow, auth, mapped['thread_id'], 'operator')
                selected = thread or await self._new_thread(uow, auth, agent_id, now)
                if source_thread:
                    mapping = dict(
                        request_hash=digest({'agent_id': agent_id}),
                        response_ref=canonical(
                            {'agent_id': agent_id, 'thread_id': selected['thread_id']}
                        ),
                        expires_at=now + 90 * 24 * 3600 * 1000,
                        updated_at=now,
                    )
                    if thread_map:
                        await self.store.replace_source(uow, thread_map['source_event_id'], mapping)
                    else:
                        await self.store.insert(
                            uow,
                            'source_events',
                            dict(
                                team_id=auth.team_id,
                                source_event_id=str(uuid4()),
                                connector_id=map_namespace,
                                external_event_id=source_thread,
                                created_at=now,
                                **mapping,
                            ),
                        )
                run_id, message_id = str(uuid4()), str(uuid4())
                accepted = AcceptedRun(
                    run_id=run_id,
                    thread_id=selected['thread_id'],
                    accepted_at=timestamp(now),
                    links=run_links(run_id),
                ).model_dump(mode='json')
                await self.store.insert(
                    uow,
                    'runs',
                    dict(
                        team_id=auth.team_id,
                        run_id=run_id,
                        thread_id=selected['thread_id'],
                        agent_id=agent_id,
                        status='queued',
                        phase=None,
                        runtime_profile_id=context.agent_profile.resource_id,
                        versions=context.model_dump_json(),
                        budgets=canonical(budgets),
                        revision=1,
                        last_event_seq=0,
                        wall_deadline_at=now + 48 * 3600 * 1000,
                        created_at=now,
                        updated_at=now,
                    ),
                )
                telemetry = current_telemetry(self.backend)
                parent = telemetry.traceparent() if telemetry is not None else None
                if parent:
                    from workbench_agent.persistence import schema

                    await uow.execute(
                        schema.run_trace_contexts.insert().values(
                            team_id=auth.team_id, run_id=run_id, traceparent=parent, created_at=now
                        )
                    )
                if query_scope is not None:
                    await self.contexts.query_scope(uow, auth, run_id, query_scope)
                # Admission fixes the policy even if no reservation ever succeeds.
                # The policy and accepted Run cannot be committed independently.
                await self.backend.usage.insert(
                    uow,
                    'usage_budget_policies',
                    dict(
                        team_id=auth.team_id,
                        run_id=run_id,
                        summary_tokens=summary_tokens,
                        created_at=now,
                    ),
                )
                await self.store.insert(
                    uow,
                    'messages',
                    dict(
                        team_id=auth.team_id,
                        message_id=message_id,
                        thread_id=selected['thread_id'],
                        run_id=run_id,
                        role='user',
                        content_ref=canonical(payload),
                        source_id=None,
                        created_at=now,
                        updated_at=now,
                    ),
                )
                await self.store.insert(
                    uow,
                    'run_jobs',
                    dict(
                        team_id=auth.team_id,
                        job_id=str(uuid4()),
                        run_id=run_id,
                        generation=1,
                        kind='run',
                        status='ready',
                        available_at=now,
                        capabilities='[]',
                        created_at=now,
                        updated_at=now,
                    ),
                )
                event_payload = {
                    'mode': request.mode,
                    'phase': None,
                    'attempt': 0,
                    'versions': context.model_dump(
                        mode='json',
                        include={
                            'agent_profile',
                            'graph_id',
                            'default_model',
                            'role_models',
                            'skills',
                            'tools',
                            'repositories',
                        },
                    ),
                }
                await self.backend.execution_events.append_control(
                    uow, auth.team_id, run_id, 'run.accepted', event_payload
                )
                if namespace:
                    values = dict(
                        request_hash=digest(payload),
                        response_ref=canonical({'scope': scope, 'accepted': accepted}),
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
                                external_event_id=request.client_message_id,
                                created_at=now,
                                **values,
                            ),
                        )
                return accepted

            result = await self.idempotency.execute(
                uow,
                auth,
                agent_id,
                'run.create',
                idempotency_key,
                payload,
                operation,
                status=202,
                max_payload_bytes=None,
            )
        # The public call commits before returning; an internal caller owns _uow commit.
        return AcceptedRun(**result.body, replayed=result.replayed)

    async def get(self, auth, run_id):
        async with self.backend.create_uow().begin_read() as uow:
            return await self.view(uow, auth, run_id)

    async def view(self, uow, auth, run_id):
        row = await self.store.one(uow, 'runs', team_id=auth.team_id, run_id=run_id)
        if not row:
            raise not_found()
        await self.authorization.current(uow, auth, row['agent_id'])
        context = RunContext.model_validate_json(row['versions'])
        now = await self.backend.clock.now_ms(uow)
        from workbench_agent.domain.transitions import active_elapsed

        waiting = await self.store.waiting(uow, auth.team_id, run_id)
        if waiting:
            descriptor = json.loads(waiting['schema'])
            waiting = dict(
                descriptor,
                interrupt_id=waiting['interrupt_id'],
                revision=waiting['revision'],
                expires_at_ms=waiting['expires_at'],
                created_at_ms=waiting['created_at'],
            )
        outcome = await self.backend.results.get(uow, auth.team_id, run_id)
        from workbench_agent.application.disclosure import project_outcome

        public_outcome = (
            await project_outcome(self.backend, uow, row, auth, json.loads(outcome['payload']))
            if outcome
            else None
        )
        links = run_links(run_id)
        gap_id = await self.backend.knowledge.replay_gap_id(uow, auth.team_id, run_id)
        if gap_id:
            from workbench_agent.knowledge.gaps import KnowledgeGaps

            gaps = KnowledgeGaps(self.backend)
            try:
                await gaps._authorize(uow, auth, 'operator')
                gap = await self.backend.knowledge.gap(uow, auth.team_id, gap_id)
                await gaps._visible(uow, auth, gap, 'operator')
                links.update(
                    original_run=f'/v1/runs/{gap["run_id"]}', knowledge_gap=f'/v1/kb/gaps/{gap_id}'
                )
            except DomainError as error:
                if error.status not in (403, 404):
                    raise
        return RunView(
            outcome=public_outcome,
            usage=public_outcome['usage_summary'] if public_outcome else {},
            run_id=run_id,
            thread_id=row['thread_id'],
            agent_id=row['agent_id'],
            status=row['status'],
            revision=row['revision'],
            phase=row['phase'],
            attempt=await self.backend.jobs.execution_attempt(uow, auth.team_id, run_id),
            created_at=timestamp(row['created_at']),
            started_at=timestamp(row['started_at']) if row['started_at'] is not None else None,
            active_elapsed_ms=active_elapsed(
                row, now, await self.store.active_until(uow, auth.team_id, run_id)
            ),
            waiting=waiting,
            links=links,
            context=context,
        )

    async def resume(
        self,
        auth,
        run_id,
        request,
        idempotency_key,
        *,
        source_connector=None,
        source_thread=None,
        source_event=None,
        source_payload=None,
    ):
        from workbench_agent.application.resume import ResumeService

        return await ResumeService(self).resume(
            auth,
            run_id,
            request,
            idempotency_key,
            source_connector=source_connector,
            source_thread=source_thread,
            source_event=source_event,
            source_payload=source_payload,
        )

    async def cancel(self, auth, run_id, key):
        from workbench_agent.application.cancel import CancelService

        return await CancelService(self).cancel(auth, run_id, key)

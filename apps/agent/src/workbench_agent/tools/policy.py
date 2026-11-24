"""Validation primitives for the guarded tool execution boundary."""

from workbench_agent.runtime.graphs.ids import GraphId
from jsonschema import Draft202012Validator, SchemaError

_IDENTITY_KEYS = frozenset(
    {
        'team_id',
        'agent_id',
        'subject',
        'user_id',
        'credential',
        'credential_scope',
        'secret_ref',
        'auth_ref',
        'headers',
    }
)


def effective_permissions(user, team, agent, skill, expert):
    return set(user) & set(team) & set(agent) & set(skill) & set(expert)


def arguments_within_scope(args, scope):
    return bool(scope) and all(
        isinstance(value, str) and bool(value.strip()) and args.get(key) == value
        for key, value in scope.items()
    )


def validate_arguments(binding, args):
    if not isinstance(args, dict) or _IDENTITY_KEYS.intersection(args):
        return False
    try:
        Draft202012Validator.check_schema(binding.input_schema)
        return Draft202012Validator(binding.input_schema).is_valid(args)
    except (SchemaError, ValueError, TypeError):
        return False


import asyncio
import json
import logging
import time
from uuid import uuid4
from workbench_agent.application.errors import DomainError, not_found
from workbench_agent.application.idempotency import canonical, digest
from workbench_agent.domain.context import RunContext
from workbench_agent.domain.tools import ToolBinding, ToolResult, ToolError, ActionProposal
from workbench_agent.domain.usage import ReservationRequest, ActualUsage
from workbench_agent.persistence.errors import StaleExecution
from workbench_agent.runtime.budgets.service import BudgetService
from workbench_agent.tools.mcp import (
    SchemaChanged,
    ScopeMappingChanged,
    dod_business_identity,
    MCPAdapter,
    BUSINESS_TOOLS,
    DOD_MCP_TOOLS,
)
from workbench_agent.tools.permissions import DeploymentToolPermissions


def _matching_evidence_window(window, args, name):
    if name == 'knowledge_search':
        return window is None
    from datetime import datetime

    try:
        return (
            isinstance(window, dict)
            and datetime.fromisoformat(window['start']) == datetime.fromisoformat(args['start'])
            and datetime.fromisoformat(window['end']) == datetime.fromisoformat(args['end'])
        )
    except (KeyError, ValueError, TypeError):
        return False


def load_published_binding(data):
    # Historical published values below the current safe envelope minimum cannot
    # be increased without violating the frozen contract; deny rather than crash.
    if type(data.get('max_output_bytes')) is int and data['max_output_bytes'] < 64:
        return None
    return ToolBinding.model_validate(data)


class PolicyToolExecutor:
    """Use trusted permission provider; model controls only schema-validated args."""

    def __init__(
        self, backend, transport, *, permissions=None, execution=None, expert=None, delays=(0.5, 2)
    ):
        from workbench_agent.jobs.worker import Execution, Worker

        owner = getattr(execution, '_tool_owner', None)
        if (
            permissions is not None
            or expert is not None
            or not isinstance(execution, Execution)
            or not isinstance(owner, Worker)
            or owner.backend is not backend
            or not owner._owns_tool_execution(execution)
        ):
            raise ValueError('current Worker execution authority required')
        self.backend, self.transport, self.execution = backend, transport, execution
        self.permissions = execution.tool_permissions
        if not isinstance(self.permissions, DeploymentToolPermissions):
            raise ValueError('trusted deployment tool permissions required')
        # The coordinator owns tool execution for the tool-use conversation.
        # Its role-specific capability is the knowledge tool grant; the model
        # and transport still execute under the same live Worker lease.
        self.expert = (
            'knowledge'
            if execution.claim.context.graph_id == GraphId.CONVERSATION_TOOL_USE
            else execution._expert_name
        )
        self.delays = delays
        self.budgets = BudgetService(backend)

    async def _replay(
        self,
        uow,
        guard,
        previous,
        binding_ref,
        args,
        scope_id,
        expected_business_scope=None,
        expected_knowledge_scope=None,
    ):
        if previous['tool_version'] != binding_ref.resource_id or previous['args_hash'] != digest(
            args
        ):
            raise DomainError('INVOCATION_CONFLICT', 409)
        if previous['status'] != 'succeeded' or not previous['receipt_ref']:
            return ToolResult(status='unknown', error=ToolError(code='INVOCATION_ALREADY_STARTED'))
        try:
            receipt = json.loads(previous['receipt_ref'])
            if (
                receipt.get('scope_id') != scope_id
                or receipt.get('binding') != binding_ref.model_dump(mode='json')
                or expected_business_scope is not None
                and receipt.get('business_scope') != list(expected_business_scope)
            ):
                raise ValueError('receipt does not match request')
            if receipt.get('kind') == 'mcp-empty-v1':
                if (
                    expected_knowledge_scope is not None
                    and receipt.get('knowledge_scope') != expected_knowledge_scope
                ):
                    raise ValueError('knowledge scope changed')
                return ToolResult(status='empty')
            if receipt.get('kind') != 'mcp-evidence-v1':
                raise ValueError('unsupported receipt')
            evidence_id = receipt['evidence_id']
            rows = await self.backend.results.evidence(
                uow, guard.team_id, guard.run_id, {evidence_id}
            )
            payload = await self.backend.tools.evidence_payload(
                uow, guard.team_id, guard.run_id, evidence_id
            )
            if len(rows) != 1 or rows[0]['scope_id'] != scope_id or not payload:
                raise ValueError('evidence unavailable')
            return ToolResult(
                status='ok', data=json.loads(payload['payload']), evidence_ref=evidence_id
            )
        except (ValueError, KeyError, TypeError):
            return ToolResult(
                status='unknown', error=ToolError(code='INVOCATION_RECEIPT_UNAVAILABLE')
            )

    async def _complete_empty(
        self,
        context,
        guard,
        binding,
        binding_ref,
        scope_id,
        ident,
        expected_business_scope=None,
        expected_knowledge_scope=None,
    ):
        await self.execution.check_operation()
        if not self.permissions.allows(context, binding.permission, self.expert):
            return ToolResult(status='denied', error=ToolError(code='TOOL_DENIED'))
        async with self.backend.create_uow().begin_write() as uow:
            await self.budgets._owned(uow, guard)
            current = await self.backend.runs.current_scope(uow, guard.team_id, guard.run_id)
            if current is None or current['scope_id'] != scope_id:
                raise DomainError('STALE_QUERY_SCOPE', 409)
            now = await self.backend.clock.now_ms(uow)
            receipt_data = {
                'kind': 'mcp-empty-v1',
                'scope_id': scope_id,
                'binding': binding_ref.model_dump(mode='json'),
            }
            if expected_business_scope is not None:
                receipt_data['business_scope'] = list(expected_business_scope)
            if expected_knowledge_scope is not None:
                receipt_data['knowledge_scope'] = expected_knowledge_scope
            receipt = canonical(receipt_data)
            if not await self.backend.tools.complete_invocation(
                uow, guard.team_id, guard.run_id, ident, 'succeeded', receipt, now
            ):
                raise DomainError('INVOCATION_CONFLICT', 409)
        return ToolResult(status='empty')

    async def execute(
        self,
        context,
        guard,
        invocation_id,
        binding_ref,
        args,
        *,
        expected_tool=None,
        max_logical_calls=None,
        expected_scope_id=None,
        expected_business_scope=None,
        dispatch_check=None,
    ):
        if not self.execution._tool_owner._owns_tool_execution(self.execution):
            raise DomainError('STALE_EXECUTION', 409)
        await self.execution.check_operation()
        async with self.backend.create_uow().begin_write() as uow:
            run, actual = await self.budgets._owned(uow, guard)
            if actual != context:
                raise DomainError('STALE_EXECUTION', 409)
            if binding_ref not in context.tools:
                raise not_found()
            row = await self.backend.configuration.one(
                uow,
                'tool_bindings',
                team_id=context.team_id,
                binding_id=binding_ref.resource_id,
                agent_id=context.agent_id,
                version=int(binding_ref.version),
                hash=binding_ref.content_hash,
            )
            if row is None:
                raise not_found()
            raw_binding = json.loads(row['schema'])
            if digest(raw_binding) != binding_ref.content_hash:
                raise not_found()
            binding = load_published_binding(raw_binding)
            if binding is None:
                return ToolResult(
                    status='unknown', error=ToolError(code='OUTPUT_LIMIT_UNSUPPORTED')
                )
            if expected_tool is not None and binding.tool_name != expected_tool:
                raise not_found()
            if self.execution is not None:
                from workbench_agent.jobs.worker import Execution

                if (
                    not isinstance(self.execution, Execution)
                    or self.execution.guard != guard
                    or self.execution.claim.context != context
                ):
                    raise DomainError('STALE_EXECUTION', 409)
            if context.identity_kind == 'connector' and self.execution is None:
                raise DomainError('FORBIDDEN', 403)
            if not self.permissions.allows(context, binding.permission, self.expert):
                return ToolResult(status='denied', error=ToolError(code='TOOL_DENIED'))
            previous = await self.backend.tools.invocation(
                uow, guard.team_id, guard.run_id, invocation_id
            )
            scope = await self.backend.runs.current_scope(uow, guard.team_id, guard.run_id)
            if scope is None:
                return ToolResult(status='denied', error=ToolError(code='QUERY_SCOPE_REQUIRED'))
            scope_id = scope['scope_id']
            if expected_scope_id is not None and scope_id != expected_scope_id:
                raise DomainError('STALE_QUERY_SCOPE', 409)
            values = json.loads(scope['scope_json'])
            if binding.tool_name in BUSINESS_TOOLS or binding.argument_mapping is not None:
                if not isinstance(self.transport, MCPAdapter):
                    return ToolResult(
                        status='denied', error=ToolError(code='TOOL_SCOPE_UNAVAILABLE')
                    )
                try:
                    prepare_options = {
                        'team_id': context.team_id,
                        'agent_id': context.agent_id,
                        'query_scope': values,
                        'correlation': {
                            'run_id': guard.run_id,
                            'invocation_id': invocation_id,
                            'query_scope_ref': scope_id,
                        },
                    }
                    if binding.argument_mapping is not None:
                        prepare_options.update(
                            argument_mapping=binding.argument_mapping,
                            full_schema=binding.input_schema,
                        )
                    elif binding.tool_name in DOD_MCP_TOOLS:
                        prepare_options['correlation'] = {
                            'run_id': guard.run_id,
                            'invocation_id': invocation_id,
                            'query_scope_ref': scope_id,
                        }
                    bound_args = self.transport.prepare(
                        binding.server,
                        binding.credential_scope,
                        binding.tool_name,
                        args,
                        **prepare_options,
                    )
                except PermissionError:
                    return ToolResult(
                        status='denied', error=ToolError(code='TOOL_SCOPE_UNAVAILABLE')
                    )
                if expected_business_scope is not None:
                    if binding.tool_name not in DOD_MCP_TOOLS or dod_business_identity(
                        bound_args, values
                    ) != tuple(expected_business_scope):
                        raise DomainError('BASELINE_SCOPE_CONFLICT', 409)
                try:
                    Draft202012Validator.check_schema(binding.input_schema)
                    valid = Draft202012Validator(binding.input_schema).is_valid(bound_args)
                except (SchemaError, ValueError, TypeError):
                    valid = False
                if not valid:
                    return ToolResult(status='invalid_input', error=ToolError(code='INVALID_INPUT'))
            else:
                if not validate_arguments(binding, args):
                    return ToolResult(status='invalid_input', error=ToolError(code='INVALID_INPUT'))
                if not arguments_within_scope(args, values):
                    return ToolResult(status='denied', error=ToolError(code='SCOPE_DENIED'))
            expected_knowledge_scope = (
                bound_args['scope']
                if context.graph_id
                in (
                    GraphId.KNOWLEDGE_CITATION_REVIEW,
                    GraphId.CONVERSATION_TOOL_USE,
                )
                and binding.tool_name == 'knowledge_search'
                else None
            )
            if not binding.read_only:
                return ToolResult(
                    status='ok',
                    proposed_action=ActionProposal(
                        binding_ref=binding_ref,
                        scope_id=scope_id,
                        args=args,
                        args_hash=digest(args),
                    ),
                )
            if previous:
                return await self._replay(
                    uow,
                    guard,
                    previous,
                    binding_ref,
                    args,
                    scope_id,
                    expected_business_scope,
                    expected_knowledge_scope,
                )
        for attempt in range(1, 4):
            if not self.execution._tool_owner._owns_tool_execution(self.execution):
                raise DomainError('STALE_EXECUTION', 409)
            await self.execution.check_operation()
            if not self.permissions.allows(context, binding.permission, self.expert):
                return ToolResult(status='denied', error=ToolError(code='TOOL_DENIED'))
            ident = str(uuid4())
            async with self.backend.create_uow().begin_write() as uow:
                current = await self.backend.runs.current_scope(uow, guard.team_id, guard.run_id)
                if (current['scope_id'] if current else None) != scope_id:
                    raise DomainError('STALE_QUERY_SCOPE', 409)
                previous = (
                    await self.backend.tools.invocation(
                        uow, guard.team_id, guard.run_id, invocation_id
                    )
                    if attempt == 1
                    else None
                )
                if previous:
                    return await self._replay(
                        uow,
                        guard,
                        previous,
                        binding_ref,
                        args,
                        scope_id,
                        expected_business_scope,
                        expected_knowledge_scope,
                    )
                if attempt == 1 and max_logical_calls is not None:
                    if type(max_logical_calls) is not int or max_logical_calls <= 0:
                        raise ValueError('invalid logical call limit')
                    count = await self.backend.tools.logical_invocation_count(
                        uow, guard.team_id, guard.run_id
                    )
                    if count >= max_logical_calls:
                        return ToolResult(
                            status='denied', error=ToolError(code='EXPERT_TOOL_LIMIT')
                        )
                if dispatch_check is not None:
                    await dispatch_check(uow)
                await self.budgets.reserve(
                    guard,
                    ReservationRequest(invocation_id=ident, kind='tool', tool_attempts=1),
                    uow=uow,
                )
                permit = await self.budgets.mark_sent(guard, ident, uow=uow)
                if not permit.dispatch_allowed:
                    raise DomainError('INVOCATION_ALREADY_SENT', 409)
                run, _ = await self.budgets._owned(uow, guard)
                now = await self.budgets._dispatch_deadline(uow, guard, run)
                from workbench_agent.domain.transitions import active_elapsed

                limits = json.loads(run['budgets'])
                remaining = min(
                    run['wall_deadline_at'] - now,
                    limits['wall_seconds'] * 1000
                    - active_elapsed(
                        run,
                        now,
                        await self.backend.runs.active_until(uow, guard.team_id, guard.run_id),
                    ),
                )
                timeout = min(binding.timeout_seconds, max(0, remaining / 1000))
                await self.backend.tools.insert_invocation(
                    uow,
                    dict(
                        team_id=guard.team_id,
                        invocation_id=ident,
                        run_id=guard.run_id,
                        logical_call_id=invocation_id,
                        tool_version=binding_ref.resource_id,
                        args_hash=digest(args),
                        attempt=attempt,
                        status='possibly_sent',
                        receipt_ref=None,
                        created_at=now,
                        updated_at=now,
                    ),
                )
            started = time.monotonic()
            try:
                correlation = {
                    'run_id': guard.run_id,
                    'invocation_id': invocation_id,
                    'query_scope_ref': scope_id,
                }
                options = (
                    {'correlation': correlation}
                    if binding.tool_name in DOD_MCP_TOOLS or binding.argument_mapping is not None
                    else {}
                )
                if binding.argument_mapping is not None:
                    options['argument_mapping'] = binding.argument_mapping
                if expected_business_scope is not None:
                    options['expected_business_scope'] = expected_business_scope
                if expected_knowledge_scope is not None:
                    options['expected_knowledge_scope'] = expected_knowledge_scope
                from workbench_agent.observability import span_or_none

                with span_or_none(
                    self.backend,
                    'tool.execute',
                    attributes={
                        'langfuse.observation.type': 'tool',
                        'workbench.invocation_id': ident,
                        'workbench.tool_version': binding_ref.resource_id,
                        'workbench.attempt': attempt,
                    },
                ) as span:
                    try:
                        response = await asyncio.wait_for(
                            self.transport.call(
                                binding.server,
                                binding.credential_scope,
                                binding.tool_name,
                                args,
                                team_id=context.team_id,
                                agent_id=context.agent_id,
                                expected_schema=binding.input_schema,
                                query_scope=values,
                                **options,
                            ),
                            timeout=timeout,
                        )
                    except BaseException:
                        if span is not None:
                            span.set_attribute('langfuse.observation.level', 'ERROR')
                            span.set_attribute(
                                'langfuse.observation.status_message', 'TOOL_TRANSPORT_FAILED'
                            )
                        raise
                status = 'ok'
                code = None
            except asyncio.CancelledError:
                raise
            except ScopeMappingChanged:
                response = None
                status = 'denied'
                code = 'BASELINE_SCOPE_CONFLICT'
            except PermissionError:
                response = None
                status = 'denied'
                code = 'TOOL_DENIED'
            except SchemaChanged:
                response = None
                status = 'invalid_input'
                code = 'TOOL_SCHEMA_CHANGED'
            except TimeoutError:
                response = None
                status = 'timeout'
                code = 'TOOL_TIMEOUT'
            except Exception:
                response = None
                status = 'unavailable'
                code = 'TOOL_UNAVAILABLE'
            logging.getLogger('workbench.tools').info(
                'tool transport finished',
                extra={
                    'event': 'tool.transport_completed',
                    'run_id': guard.run_id,
                    'invocation_id': ident,
                    'attempt': attempt,
                    'status': status,
                    'error_code': code,
                    'duration_ms': int((time.monotonic() - started) * 1000),
                },
            )
            try:
                async with self.backend.create_uow().begin_write() as uow:
                    await self.budgets.settle(
                        guard, ident, ActualUsage(input_tokens=0, output_tokens=0), uow=uow
                    )
            except (StaleExecution, DomainError) as error:
                if isinstance(error, DomainError) and error.code not in ('FORBIDDEN', 'NOT_FOUND'):
                    raise
                async with self.backend.create_uow().begin_write() as uow:
                    await self.budgets.quarantine(
                        guard, ident, ActualUsage(input_tokens=0, output_tokens=0), uow=uow
                    )
                raise
            if status == 'ok':
                if binding.tool_name == 'web_search':
                    from workbench_agent.tools.web_search_result import (
                        validate_web_search_response,
                    )

                    web_result = validate_web_search_response(
                        response,
                        policy=self.transport.policy,
                        team_id=context.team_id,
                        agent_id=context.agent_id,
                        binding=binding,
                    )
                    web_status = web_result['status']
                    if web_status in {'empty', 'denied'}:
                        if web_status == 'empty':
                            return await self._complete_empty(
                                context,
                                guard,
                                binding,
                                binding_ref,
                                scope_id,
                                ident,
                                expected_business_scope,
                                expected_knowledge_scope,
                            )
                        return ToolResult(
                            status='denied',
                            error=ToolError(code=web_result['error']),
                        )
                    if web_status in {'unavailable', 'unknown'}:
                        return ToolResult(
                            status=('unavailable' if web_status == 'unavailable' else 'unknown'),
                            error=ToolError(code=web_result['error']),
                        )
                    response = {key: value for key, value in web_result.items() if key != 'grant'}
                elif binding.argument_mapping is None and binding.tool_name in DOD_MCP_TOOLS:
                    result = getattr(response, 'structuredContent', None)
                    if (
                        not isinstance(result, dict)
                        or result.get('schema_version') != 'dod-tool-result-v1'
                        or result.get('status')
                        not in (
                            'ok',
                            'empty',
                            'denied',
                            'invalid_input',
                            'timeout',
                            'unavailable',
                            'unknown',
                        )
                    ):
                        return ToolResult(status='unknown', error=ToolError(code='INVALID_OUTPUT'))
                    if not isinstance(result.get('data'), dict) or not isinstance(
                        result.get('meta'), dict
                    ):
                        return ToolResult(status='unknown', error=ToolError(code='INVALID_OUTPUT'))
                    upstream = result['status']
                    if upstream == 'empty':
                        record_key = {
                            'knowledge_search': 'matches',
                            'metrics_query': 'series',
                            'logs_search': 'entries',
                            'trace_get': 'spans',
                        }[binding.tool_name]
                        if (
                            getattr(response, 'isError', False)
                            or result['data'].get(record_key) != []
                            or result['meta'].get('truncated', False) is not False
                            or binding.output_schema is not None
                            and not Draft202012Validator(binding.output_schema).is_valid(result)
                        ):
                            return ToolResult(
                                status='unknown', error=ToolError(code='INVALID_OUTPUT')
                            )
                        return await self._complete_empty(
                            context,
                            guard,
                            binding,
                            binding_ref,
                            scope_id,
                            ident,
                            expected_business_scope,
                            expected_knowledge_scope,
                        )
                    if upstream != 'ok':
                        retryable = (
                            upstream in ('timeout', 'unavailable')
                            and result.get('retryable') is True
                        )
                        if retryable and attempt < 3:
                            await asyncio.sleep(self.delays[attempt - 1])
                            continue
                        return ToolResult(
                            status=upstream,
                            error=ToolError(
                                code='MCP_QUERY_' + upstream.upper(), retryable=retryable
                            ),
                        )
                    if getattr(response, 'isError', False):
                        return ToolResult(status='unknown', error=ToolError(code='INVALID_OUTPUT'))
                    refs = result.get('evidence_refs')
                    if (
                        not isinstance(refs, list)
                        or not refs
                        or any(
                            not isinstance(item, dict)
                            or item.get('scope') != bound_args['scope']
                            or item.get('invocation_id') != invocation_id
                            or item.get('query_scope_ref') != scope_id
                            or item.get('quality_status') != 'valid'
                            or not item.get('source_locator')
                            or item.get('source_type')
                            != {
                                'knowledge_search': 'knowledge',
                                'metrics_query': 'metrics',
                                'logs_search': 'logs',
                                'trace_get': 'trace',
                            }[binding.tool_name]
                            or not _matching_evidence_window(
                                item.get('time_window'), bound_args, binding.tool_name
                            )
                            for item in refs
                        )
                    ):
                        return ToolResult(
                            status='unknown', error=ToolError(code='INVALID_EVIDENCE')
                        )
                elif getattr(response, 'isError', False):
                    return ToolResult(
                        status='unavailable', error=ToolError(code='TOOL_PROVIDER_ERROR')
                    )
                if (
                    hasattr(response, 'structuredContent')
                    and response.structuredContent is not None
                ):
                    response = response.structuredContent
                elif hasattr(response, 'model_dump'):
                    response = response.model_dump(mode='json')
                if (
                    binding.argument_mapping is not None
                    and isinstance(response, dict)
                    and response.get('structuredContent') is None
                    and isinstance(response.get('content'), list)
                ):
                    texts = [
                        item.get('text')
                        for item in response['content']
                        if isinstance(item, dict)
                        and item.get('type') == 'text'
                        and isinstance(item.get('text'), str)
                    ]
                    if len(texts) == 1:
                        try:
                            response = json.loads(texts[0])
                        except ValueError:
                            response = None
                if response in (None, [], {}, ''):
                    return await self._complete_empty(
                        context,
                        guard,
                        binding,
                        binding_ref,
                        scope_id,
                        ident,
                        expected_business_scope,
                        expected_knowledge_scope,
                    )
                if binding.output_schema is not None and not Draft202012Validator(
                    binding.output_schema
                ).is_valid(response):
                    return ToolResult(status='unknown', error=ToolError(code='INVALID_OUTPUT'))
                if scope_id is None:
                    return ToolResult(
                        status='unknown', error=ToolError(code='QUERY_SCOPE_REQUIRED')
                    )
                await self.execution.check_operation()
                if not self.permissions.allows(context, binding.permission, self.expert):
                    return ToolResult(status='denied', error=ToolError(code='TOOL_DENIED'))
                from workbench_agent.domain.evidence import safe_evidence_payload

                payload, truncated = safe_evidence_payload(response, binding.max_output_bytes)
                if (
                    binding.tool_name in DOD_MCP_TOOLS
                    and isinstance(response, dict)
                    and response.get('meta', {}).get('truncated') is True
                ):
                    truncated = True
                async with self.backend.create_uow().begin_write() as uow:
                    await self.budgets._owned(uow, guard)
                    current = await self.backend.runs.current_scope(
                        uow, guard.team_id, guard.run_id
                    )
                    if current is None or current['scope_id'] != scope_id:
                        raise DomainError('STALE_QUERY_SCOPE', 409)
                    evidence_id = str(uuid4())
                    now = await self.backend.clock.now_ms(uow)
                    await self.backend.tools.insert_evidence(
                        uow,
                        dict(
                            team_id=guard.team_id,
                            evidence_id=evidence_id,
                            run_id=guard.run_id,
                            scope_id=scope_id,
                            source=binding.alias,
                            quality='truncated' if truncated else 'valid',
                            time_window=canonical(
                                {'observed_at_ms': now}
                                if binding.tool_name == 'Common-DoD-GetInfo'
                                else {
                                    key: values[key]
                                    for key in ('start_at', 'end_at')
                                    if key in values
                                }
                            ),
                            artifact_ref=None,
                            summary=digest(response),
                            created_at=now,
                            updated_at=now,
                        ),
                    )
                    await self.backend.tools.insert_evidence_payload(
                        uow,
                        dict(
                            team_id=guard.team_id,
                            evidence_id=evidence_id,
                            run_id=guard.run_id,
                            payload=payload,
                            truncated=int(truncated),
                            created_at=now,
                        ),
                    )
                    receipt_data = {
                        'kind': 'mcp-evidence-v1',
                        'scope_id': scope_id,
                        'evidence_id': evidence_id,
                        'binding': binding_ref.model_dump(mode='json'),
                    }
                    if expected_business_scope is not None:
                        receipt_data['business_scope'] = list(expected_business_scope)
                    receipt = canonical(receipt_data)
                    if not await self.backend.tools.complete_invocation(
                        uow, guard.team_id, guard.run_id, ident, 'succeeded', receipt, now
                    ):
                        raise DomainError('INVOCATION_CONFLICT', 409)
                return ToolResult(status='ok', data=json.loads(payload), evidence_ref=evidence_id)
            if status in ('denied', 'invalid_input'):
                return ToolResult(status=status, error=ToolError(code=code))
            if attempt < 3:
                await asyncio.sleep(self.delays[attempt - 1])
        return ToolResult(status=status, error=ToolError(code=code, retryable=True))

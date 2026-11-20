"""Experimental bounded tool conversation over published read-only MCP tools."""

from __future__ import annotations

import json
from typing import TypedDict
from uuid import UUID, uuid5

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from workbench_agent.application.errors import DomainError
from workbench_agent.domain.identity import AuthContext
from workbench_agent.jobs.worker import CapabilityBlocked
from workbench_agent.runtime.budgets.service import BudgetService
from workbench_agent.runtime.graphs.agent_instructions import system_instruction
from workbench_agent.runtime.graphs.ids import GraphId
from workbench_agent.tools.model_schema import project_model_schema


STATE_SCHEMA_VERSION = 'conversation-tool-use-state-v1'
NOTICE = '以下回答基于所列工具返回的结果，未经独立校验\n\n'
MAX_TOOL_CALLS = 4


class ToolConversationState(TypedDict, total=False):
    run_id: str
    input_text: str
    mode: str
    phase: str
    messages: list
    scope_id: str
    bindings: list[dict]
    tool_calls_used: int
    pending_calls: list[dict]
    call_results: list[dict]
    evidence_refs: list[str]
    last_error: str
    outcome: dict


MAX_HISTORY_BYTES = 16_000


async def _authorized_history(execution, *, limit: int):
    import json
    from langchain_core.messages import AIMessage, HumanMessage
    from sqlalchemy import and_, select
    from workbench_agent.application.context import ContextResolver
    from workbench_agent.domain.context import RunContext
    from workbench_agent.domain.outcomes import RunOutcome
    from workbench_agent.persistence import schema
    from workbench_agent.runtime.graphs.evidence_authority import authorized_sources

    if limit <= 0:
        return []
    backend, guard, context = execution.backend, execution.guard, execution.claim.context
    async with backend.create_uow().begin_read() as uow:
        await ContextResolver(backend).revalidate(context, uow=uow)
        current = await backend.runs.one(
            uow, 'runs', team_id=guard.team_id, run_id=guard.run_id
        )
        scope = await backend.runs.current_scope(uow, guard.team_id, guard.run_id)
        if not current or not scope:
            return []
        runs, outcomes = schema.runs, schema.run_outcomes
        rows = (await uow.execute(
            select(
                runs,
                outcomes.c.payload.label('result_payload'),
                outcomes.c.digest.label('result_digest'),
                outcomes.c.message_id.label('result_message_id'),
            )
            .join(outcomes, and_(
                outcomes.c.team_id == runs.c.team_id,
                outcomes.c.run_id == runs.c.run_id,
            ))
            .where(
                runs.c.team_id == guard.team_id,
                runs.c.agent_id == context.agent_id,
                runs.c.thread_id == guard.thread_id,
                runs.c.run_id != guard.run_id,
                runs.c.status == 'succeeded',
                runs.c.created_at < current['created_at'],
            )
            .order_by(runs.c.created_at.desc(), runs.c.run_id.desc())
            .limit(64)
        )).mappings().all()
        turns = []
        for row in reversed(rows):
            try:
                prior = RunContext.model_validate_json(row['versions'])
                prior_scope = await backend.runs.current_scope(
                    uow, guard.team_id, row['run_id']
                )
                if (
                    prior.graph_id != GraphId.CONVERSATION_TOOL_USE
                    or prior.team_id != guard.team_id
                    or prior.agent_id != context.agent_id
                    or not prior_scope
                    or prior_scope['hash'] != scope['hash']
                ):
                    continue
                outcome = RunOutcome.model_validate_json(row['result_payload'])
                if (
                    outcome.digest() != row['result_digest']
                    or not outcome.completed
                    or outcome.kind != 'analysis.v1'
                    or not outcome.summary.startswith(NOTICE)
                ):
                    continue
                if outcome.evidence_refs:
                    allowed, scope_id = await authorized_sources(
                        backend,
                        uow,
                        type('Guard', (), {
                            'team_id': guard.team_id,
                            'run_id': row['run_id'],
                        })(),
                        prior,
                        {str(ref) for ref in outcome.evidence_refs},
                        expected_scope=prior_scope['scope_id'],
                    )
                    if set(allowed) != {str(ref) for ref in outcome.evidence_refs}:
                        continue
                stored = await backend.runs.one(
                    uow,
                    'messages',
                    team_id=guard.team_id,
                    run_id=row['run_id'],
                    role='user',
                )
                if not stored:
                    continue
                request = json.loads(stored['content_ref'])
                question = request.get('input', {}).get('text')
                answer = outcome.summary[len(NOTICE):]
                if not isinstance(question, str) or not question.strip() or not answer.strip():
                    continue
                turns.append((
                    HumanMessage(content=question),
                    AIMessage(content=answer),
                ))
            except (ValueError, TypeError, KeyError, AttributeError):
                continue
        selected = []
        total = 0
        for turn in reversed(turns):
            size = sum(len(message.content.encode('utf-8')) for message in turn)
            if len(selected) >= limit or total + size > MAX_HISTORY_BYTES:
                break
            selected.insert(0, turn)
            total += size
        return selected


async def _load_bindings(execution):
    import json
    from workbench_agent.application.idempotency import digest
    from workbench_agent.tools.policy import load_published_binding
    context = execution.claim.context
    result = []
    if len(context.tools) > MAX_TOOL_CALLS:
        raise DomainError('TOOL_BINDINGS_INVALID', 422)
    async with execution.backend.create_uow().begin_read() as uow:
        for ref in context.tools:
            row = await execution.backend.configuration.one(
                uow,
                'tool_bindings',
                team_id=context.team_id,
                agent_id=context.agent_id,
                binding_id=ref.resource_id,
                version=int(ref.version),
                hash=ref.content_hash,
            )
            if not row:
                raise DomainError('TOOL_BINDINGS_INVALID', 422)
            raw = json.loads(row['schema'])
            if digest(raw) != ref.content_hash:
                raise DomainError('TOOL_BINDINGS_INVALID', 422)
            binding = load_published_binding(raw)
            if (
                binding is None
                or not binding.read_only
                or binding.tool_name != 'knowledge_search'
            ):
                raise DomainError('TOOL_BINDINGS_INVALID', 422)
            result.append({
                'ref': ref.model_dump(mode='json'),
                'alias': binding.alias,
                'server': binding.server,
                'credential_scope': binding.credential_scope,
                'permission': binding.permission,
                'tool_name': binding.tool_name,
                'schema': project_model_schema(binding.tool_name, binding.input_schema),
            })
    return result


async def _model(execution, messages, *, phase, bindings):
    factory = getattr(execution.backend, '_diagnosis_factory', None)
    if factory is None:
        raise CapabilityBlocked('TOOL_CAPABILITY_UNAVAILABLE')
    invoker, _ = factory(execution)
    tools = []
    from langchain_core.tools import StructuredTool
    from pydantic import create_model
    for binding in bindings:
        fields = {}
        for name, spec in binding['schema']['properties'].items():
            kinds = {'string': str, 'integer': int, 'number': float, 'boolean': bool}
            required = name in binding['schema'].get('required', [])
            fields[name] = (kinds[spec['type']], ...) if required else (kinds[spec['type']] | None, None)
        args_schema = create_model(f"Tool_{binding['alias'].replace('.', '_')}", **fields)
        async def _unused(**kwargs):
            return '{}'
        tools.append(StructuredTool(
            name=binding['alias'].replace('.', '_'),
            description='Read-only scoped knowledge tool',
            args_schema=args_schema,
            coroutine=_unused,
        ))
    if not tools:
        return await invoker.invoke(
            execution.guard,
            'knowledge',
            messages,
            logical_call_id=str(uuid5(
                UUID(execution.guard.run_id),
                f'{GraphId.CONVERSATION_TOOL_USE}:{phase}:no-tools',
            )),
        )
    return await invoker.invoke(
        execution.guard,
        'knowledge',
        messages,
        tools=tools,
        logical_call_id=None,
    )


def build_tool_conversation(saver):
    async def prepare(state, config, runtime: Runtime):
        execution = runtime.context
        await execution.check_operation()
        if (
            state['run_id'] != config['configurable']['thread_id']
            or state.get('mode') != 'qa'
        ):
            raise ValueError('invalid tool conversation input')
        bindings = await _load_bindings(execution)
        history = await _authorized_history(
            execution, limit=execution.claim.context.conversation_history_turns
        )
        history_messages = [
            message for pair in history for message in pair
        ]
        return {
            'phase': 'prepare',
            'bindings': [
                {
                    'ref': item['ref'],
                    'alias': item['alias'],
                    'tool_name': item['tool_name'],
                    'schema': item['schema'],
                }
                for item in bindings
            ],
            'tool_calls_used': 0,
            'messages': [
                SystemMessage(content=system_instruction(execution.claim.context)),
                *history_messages,
                HumanMessage(content=state['input_text']),
            ],
        }

    async def model(state, runtime: Runtime):
        execution = runtime.context
        await execution.check_operation()
        try:
            bindings = await _load_bindings(execution)
            result = await _model(
                execution,
                state['messages'],
                phase='model',
                bindings=bindings,
            )
        except DomainError as exc:
            if exc.code in {
                'BUDGET_EXCEEDED',
                'SUMMARY_RESERVE_REQUIRED',
                'MAX_CONTEXT_EXCEEDED',
                'MODEL_PROFILE_UNAVAILABLE',
                'MODEL_CREDENTIAL_UNAVAILABLE',
            }:
                return {'phase': 'model', 'last_error': exc.code}
            raise
        if result.status != 'ok' or not isinstance(result.message, AIMessage):
            return {
                'phase': 'model',
                'last_error': result.error_code or 'MODEL_OUTPUT_UNAVAILABLE',
            }
        return {
            'phase': 'model',
            'messages': [*state['messages'], result.message],
            'last_error': '',
        }

    async def tools(state, runtime: Runtime):
        execution = runtime.context
        await execution.check_operation()
        message = state['messages'][-1] if state.get('messages') else None
        calls = getattr(message, 'tool_calls', ()) if message is not None else ()
        remaining = MAX_TOOL_CALLS - state.get('tool_calls_used', 0)
        if len(calls) > remaining:
            from langchain_core.messages import ToolMessage
            limited = [
                ToolMessage(
                    content=json.dumps({
                        'status': 'denied',
                        'error': {'code': 'TOOL_CALL_LIMIT'},
                    }),
                    name=call.get('name'),
                    tool_call_id=call.get('id'),
                )
                for call in calls[remaining:]
            ]
            results = list(state.get('call_results', []))
            limited_calls = calls[remaining:]
            for call in limited_calls:
                results.append({
                    'tool_call_id': call.get('id'),
                    'alias': call.get('name'),
                    'status': 'denied',
                    'payload': {
                        'status': 'denied',
                        'error': {'code': 'TOOL_CALL_LIMIT'},
                    },
                })
            return {
                'phase': 'tools',
                'last_error': 'TOOL_CALL_LIMIT',
                'call_results': results,
                'messages': [
                    *state['messages'],
                    *limited,
                ],
            }
        bindings = {item['alias'].replace('.', '_'): item for item in state['bindings']}
        factory = getattr(execution.backend, '_diagnosis_factory', None)
        if factory is None:
            return {'phase': 'tools', 'last_error': 'TOOL_CAPABILITY_UNAVAILABLE'}
        _, executor = factory(execution)
        scope = None
        async with execution.backend.create_uow().begin_read() as uow:
            scope = await execution.backend.runs.current_scope(
                uow, execution.guard.team_id, state['run_id']
            )
        if not scope:
            return {'phase': 'tools', 'last_error': 'QUERY_SCOPE_REQUIRED'}
        from langchain_core.messages import ToolMessage
        messages = list(state['messages'])
        results = list(state.get('call_results', []))
        used = state.get('tool_calls_used', 0)
        for call in calls[:remaining]:
            binding = bindings.get(call.get('name'))
            if binding is None:
                payload = {'status': 'denied', 'error': {'code': 'UNKNOWN_TOOL'}}
            else:
                ref = binding['ref']
                from workbench_agent.domain.profiles import VersionRef
                invocation = str(uuid5(
                    UUID(state['run_id']),
                    f'{GraphId.CONVERSATION_TOOL_USE}:tool:{call.get("id")}',
                ))
                try:
                    output = await executor.execute(
                        execution.claim.context,
                        execution.guard,
                        invocation,
                        VersionRef.model_validate(ref),
                        call.get('args') or {},
                        expected_tool='knowledge_search',
                        max_logical_calls=MAX_TOOL_CALLS,
                        expected_scope_id=scope['scope_id'],
                    )
                    payload = output.model_dump(mode='json')
                except DomainError as exc:
                    payload = {'status': 'denied', 'error': {'code': exc.code}}
            results.append({
                'tool_call_id': call.get('id'),
                'alias': binding['alias'] if binding else call.get('name'),
                'status': payload.get('status', 'unknown'),
                'payload': payload,
            })
            messages.append(
                ToolMessage(
                    content=json.dumps(payload, sort_keys=True),
                    name=call.get('name'),
                    tool_call_id=call.get('id'),
                )
            )
            used += 1
        return {
            'phase': 'tools',
            'messages': messages,
            'call_results': results,
            'tool_calls_used': used,
        }

    async def compose(state, runtime: Runtime):
        execution = runtime.context
        await execution.check_operation()
        context = execution.claim.context
        auth = AuthContext(
            team_id=context.team_id,
            subject=context.subject,
            roles=context.roles,
            identity_kind=context.identity_kind,
            agent_scope=context.agent_scope,
        )
        outcome = (
            await BudgetService(execution.backend).partial_outcome(
                auth, state['run_id']
            )
        ).model_dump(mode='json')
        results = state.get('call_results', [])
        evidence = [
            item['payload']['evidence_ref']
            for item in results
            if item.get('payload', {}).get('status') == 'ok'
            and item['payload'].get('evidence_ref')
        ]
        if not results:
            return {
                'phase': 'compose',
                'outcome': {
                    **outcome,
                    'completed': False,
                    'summary': '本轮未调用工具，缺少工具依据。',
                    'unresolved_items': ['本轮未调用工具，缺少工具依据。'],
                    'evidence_refs': [],
                },
            }
        model_message = next(
            (
                message
                for message in reversed(state.get('messages', []))
                if isinstance(message, AIMessage) and isinstance(message.content, str)
            ),
            None,
        )
        if model_message is None:
            return {
                'phase': 'compose',
                'outcome': {
                    **outcome,
                    'completed': False,
                    'summary': '工具对话未完成。',
                    'unresolved_items': [
                        state.get('last_error') or 'MODEL_OUTPUT_UNAVAILABLE'
                    ],
                    'evidence_refs': evidence,
                },
            }
        unresolved = []
        for item in results:
            if item['status'] != 'ok':
                unresolved.append(f'{item["alias"]}: {item["status"]}')
        if not evidence:
            unresolved.append('没有当前可访问的工具证据。')
        basis = '\n'.join(
            f'- {item["alias"]} / {item["tool_call_id"]} / {item["status"]}'
            for item in results
        )
        summary = NOTICE + model_message.content + '\n\n工具依据：\n' + basis
        return {
            'phase': 'compose',
            'outcome': {
                **outcome,
                'completed': not unresolved,
                'summary': summary,
                'unresolved_items': unresolved,
                'evidence_refs': evidence,
            },
        }

    graph = StateGraph(ToolConversationState)
    graph.add_node('prepare', prepare)
    graph.add_node('model', model)
    graph.add_node('tools', tools)
    graph.add_node('compose', compose)
    graph.add_edge(START, 'prepare')
    graph.add_edge('prepare', 'model')
    graph.add_conditional_edges(
        'model',
        lambda state: 'tools'
        if (
            state.get('tool_calls_used', 0) < MAX_TOOL_CALLS
            and state.get('messages')
            and getattr(state['messages'][-1], 'tool_calls', ())
        )
        else 'compose',
        {'tools': 'tools', 'compose': 'compose'},
    )
    graph.add_edge('tools', 'model')
    graph.add_edge('compose', END)
    return graph.compile(checkpointer=saver)

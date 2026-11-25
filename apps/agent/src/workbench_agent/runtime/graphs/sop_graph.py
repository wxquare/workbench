"""Pinned workflow execution with a guarded, checkpointed step boundary."""

from workbench_agent.runtime.graphs.ids import GraphId
import asyncio
import json
from graphlib import TopologicalSorter
from typing import TypedDict
from uuid import UUID, uuid5
from langgraph.graph import StateGraph, START, END
from langgraph.runtime import Runtime
from workbench_agent.application.idempotency import digest
from workbench_agent.domain.identity import AuthContext
from workbench_agent.domain.workflows import WorkflowDefinition
from workbench_agent.runtime.budgets.service import BudgetService

STATE_SCHEMA_VERSION = 'dod-sop-v1'


class SopState(TypedDict, total=False):
    run_id: str
    mode: str
    order: list[str]
    index: int
    results: dict
    stopped: bool
    missing: bool
    scope_ref: str
    business_scope: list[str]
    outcome: dict


async def _load(execution):
    ref = execution.claim.context.workflow
    if ref is None:
        return None
    async with execution.backend.create_uow().begin_read() as uow:
        row = await execution.backend.configuration.one(
            uow,
            'workflow_versions',
            team_id=execution.guard.team_id,
            agent_id=execution.claim.context.agent_id,
            workflow_version_id=ref.resource_id,
        )
    if not row or str(row['version']) != ref.version or row['hash'] != ref.content_hash:
        return None
    try:
        data = json.loads(row['definition_json'])
        if digest(data) != ref.content_hash:
            return None
        return WorkflowDefinition.model_validate(data)
    except (ValueError, TypeError):
        return None


async def _business_authority(execution, wf, scope, executor, uow):
    """Resolve every tool through current trusted grants before any dispatch."""
    from workbench_agent.tools.mcp import MCPAdapter, SCOPED_READ_TOOLS, scope_identity
    from workbench_agent.tools.policy import load_published_binding

    if not isinstance(executor.transport, MCPAdapter):
        # Unit-test transports are explicitly injected; production composition
        # creates MCPAdapter only. Never infer business identity from an MCP grant.
        return None
    values = json.loads(scope['scope_json'])
    identities = set()
    for item in wf.steps:
        matches = []
        for ref in execution.claim.context.tools:
            row = await execution.backend.configuration.one(
                uow,
                'tool_bindings',
                team_id=execution.guard.team_id,
                agent_id=execution.claim.context.agent_id,
                binding_id=ref.resource_id,
                version=int(ref.version),
                hash=ref.content_hash,
            )
            if not row:
                continue
            data = json.loads(row['schema'])
            binding = load_published_binding(data) if digest(data) == ref.content_hash else None
            if binding and binding.alias == item.tool and binding.read_only:
                matches.append(binding)
        if len(matches) != 1 or matches[0].tool_name not in SCOPED_READ_TOOLS:
            raise PermissionError('SOP business scope cannot be verified')
        binding = matches[0]
        args = dict(item.arguments)
        args.update({key: 'scope-preflight' for key in item.inputs})
        mapped = executor.transport.prepare(
            binding.server,
            binding.credential_scope,
            binding.tool_name,
            args,
            team_id=execution.guard.team_id,
            agent_id=execution.claim.context.agent_id,
            query_scope=values,
            correlation={
                'run_id': execution.guard.run_id,
                'invocation_id': 'sop-scope-check',
                'query_scope_ref': scope['scope_id'],
            },
        )
        identities.add(scope_identity(mapped, values))
    if len(identities) != 1:
        raise PermissionError('SOP business scope mismatch')
    return next(iter(identities))


def build_sop(saver, *, version=GraphId.SOP_CONDITIONAL):
    async def prepare(state, config, runtime: Runtime):
        await runtime.context.check_operation()
        if state['run_id'] != config['configurable']['thread_id'] or state.get('mode') not in (
            'sop',
            'inspection',
        ):
            raise ValueError('invalid SOP invocation')
        wf = await _load(runtime.context)
        if wf is None:
            return {'missing': True, 'order': [], 'index': 0, 'results': {}}
        order = list(
            TopologicalSorter({s.step_id: set(s.depends_on) for s in wf.steps}).static_order()
        )
        execution = runtime.context
        factory = getattr(execution.backend, '_sop_factory', None) or getattr(
            execution.backend, '_baseline_factory', None
        )
        try:
            executor = factory(execution) if factory else None
            from workbench_agent.tools.policy import PolicyToolExecutor

            if not isinstance(executor, PolicyToolExecutor) or executor.execution is not execution:
                raise PermissionError()
            async with execution.backend.create_uow().begin_read() as uow:
                scope = await execution.backend.runs.current_scope(
                    uow, execution.guard.team_id, state['run_id']
                )
                if not scope:
                    raise PermissionError()
                authority = await _business_authority(execution, wf, scope, executor, uow)
        except (PermissionError, ValueError, TypeError):
            return {'missing': True, 'order': [], 'index': 0, 'results': {}}
        return {
            'order': order,
            'index': 0,
            'results': {},
            'stopped': False,
            'scope_ref': scope['scope_id'],
            'business_scope': list(authority) if authority else [],
        }

    async def step(state, runtime: Runtime):
        from workbench_agent.tools.policy import PolicyToolExecutor
        from workbench_agent.tools.policy import load_published_binding

        execution = runtime.context
        await execution.check_operation()
        wf = await _load(execution)
        if wf is None:
            return {'missing': True, 'index': len(state['order'])}
        item = next(s for s in wf.steps if s.step_id == state['order'][state['index']])
        results = dict(state.get('results', {}))
        refs = {entry['evidence'] for entry in results.values() if entry.get('evidence')}
        from workbench_agent.runtime.graphs.evidence_authority import authorized_sources

        async with execution.backend.create_uow().begin_write() as uow:
            await execution.backend.lease_guard.require(uow, execution.guard)
            allowed, current_scope = await authorized_sources(
                execution.backend,
                uow,
                execution.guard,
                execution.claim.context,
                refs,
                expected_scope=state['scope_ref'],
            )
        authority_lost = current_scope != state['scope_ref'] or set(allowed) != refs
        status, output, evidence = 'blocked', {}, None
        allowed_statuses = ('ok', 'skipped') if version == GraphId.SOP_CONDITIONAL else ('ok',)
        if (
            not authority_lost
            and not state.get('stopped')
            and all(results[dep]['status'] in allowed_statuses for dep in item.depends_on)
        ):
            if version == GraphId.SOP_CONDITIONAL and item.condition is not None:
                parent, field = item.condition.source.split('.')
                prior = results[parent]['output']
                if field not in prior:
                    results[item.step_id] = {
                        'status': 'invalid_input',
                        'output': {},
                        'evidence': None,
                    }
                    return {
                        'index': state['index'] + 1,
                        'results': results,
                        'stopped': item.failure_policy == 'stop',
                    }
                value = prior[field]
                equal = (
                    type(value) is type(item.condition.equals) and value == item.condition.equals
                )
                if equal == item.condition.negate:
                    results[item.step_id] = {'status': 'skipped', 'output': {}, 'evidence': None}
                    return {'index': state['index'] + 1, 'results': results, 'stopped': False}
            args = dict(item.arguments)
            for target, source in item.inputs.items():
                parent, field = source.split('.')
                if field not in results[parent]['output']:
                    status = 'invalid_input'
                    break
                args[target] = results[parent]['output'][field]
            else:
                factory = getattr(execution.backend, '_sop_factory', None) or getattr(
                    execution.backend, '_baseline_factory', None
                )
                matches = []
                async with execution.backend.create_uow().begin_read() as uow:
                    scope = await execution.backend.runs.current_scope(
                        uow, execution.guard.team_id, state['run_id']
                    )
                    for ref in execution.claim.context.tools:
                        row = await execution.backend.configuration.one(
                            uow,
                            'tool_bindings',
                            team_id=execution.guard.team_id,
                            agent_id=execution.claim.context.agent_id,
                            binding_id=ref.resource_id,
                            version=int(ref.version),
                            hash=ref.content_hash,
                        )
                        if row:
                            data = json.loads(row['schema'])
                            binding = (
                                load_published_binding(data)
                                if digest(data) == ref.content_hash
                                else None
                            )
                            if binding and binding.alias == item.tool and binding.read_only:
                                matches.append(ref)
                status = 'unavailable'
                if factory and scope and len(matches) == 1:
                    try:
                        executor = factory(execution)
                        if (
                            not isinstance(executor, PolicyToolExecutor)
                            or executor.execution is not execution
                        ):
                            raise ValueError('untrusted executor')
                        from workbench_agent.tools.mcp import MCPAdapter

                        if isinstance(executor.transport, MCPAdapter) and not state.get(
                            'business_scope'
                        ):
                            raise PermissionError('missing persisted SOP business scope')

                        async def dispatch_check(uow):
                            from workbench_agent.application.errors import DomainError

                            await execution.backend.lease_guard.require(uow, execution.guard)
                            current, current_id = await authorized_sources(
                                execution.backend,
                                uow,
                                execution.guard,
                                execution.claim.context,
                                refs,
                                expected_scope=state['scope_ref'],
                            )
                            if current_id != state['scope_ref'] or set(current) != refs:
                                raise DomainError('SOP_SOURCE_ACCESS_REVOKED', 409)
                            if state.get('business_scope'):
                                scope_now = await execution.backend.runs.current_scope(
                                    uow, execution.guard.team_id, state['run_id']
                                )
                                try:
                                    authority = await _business_authority(
                                        execution, wf, scope_now, executor, uow
                                    )
                                except PermissionError:
                                    raise DomainError('SOP_SOURCE_ACCESS_REVOKED', 409) from None
                                if list(authority or ()) != state['business_scope']:
                                    raise DomainError('SOP_SOURCE_ACCESS_REVOKED', 409)

                        result = await asyncio.wait_for(
                            executor.execute(
                                execution.claim.context,
                                execution.guard,
                                str(
                                    uuid5(UUID(state['run_id']), version + ':step:' + item.step_id)
                                ),
                                matches[0],
                                args,
                                expected_scope_id=state['scope_ref'],
                                expected_business_scope=state.get('business_scope') or None,
                                dispatch_check=dispatch_check,
                            ),
                            timeout=item.timeout_seconds,
                        )
                        status = result.status.value
                        output = (
                            result.data if status == 'ok' and isinstance(result.data, dict) else {}
                        )
                        evidence = result.evidence_ref
                        if (
                            version == GraphId.SOP_CONDITIONAL
                            and status == 'ok'
                            and item.output_schema is not None
                        ):
                            from jsonschema import Draft202012Validator

                            if not Draft202012Validator(item.output_schema).is_valid(output):
                                status, output, evidence = 'invalid_output', {}, None
                    except TimeoutError:
                        status = 'timeout'
                    except (ValueError, PermissionError):
                        status = 'unavailable'
                    except Exception as error:
                        from workbench_agent.application.errors import DomainError

                        if isinstance(error, DomainError) and error.code in (
                            'BUDGET_EXCEEDED',
                            'STALE_QUERY_SCOPE',
                            'SOP_SOURCE_ACCESS_REVOKED',
                            'BASELINE_SCOPE_CONFLICT',
                        ):
                            status = (
                                'blocked'
                                if error.code == 'SOP_SOURCE_ACCESS_REVOKED'
                                else 'unavailable'
                            )
                        else:
                            raise
        results[item.step_id] = {'status': status, 'output': output, 'evidence': evidence}
        stopped = (
            authority_lost
            or state.get('stopped', False)
            or (status != 'ok' and item.failure_policy == 'stop')
        )
        return {'index': state['index'] + 1, 'results': results, 'stopped': stopped}

    def route(state):
        return (
            'step'
            if state['index'] < len(state['order']) and not state.get('missing')
            else 'compose'
        )

    async def compose(state, runtime: Runtime):
        await runtime.context.check_operation()
        ctx = runtime.context.claim.context
        auth = AuthContext(
            team_id=ctx.team_id,
            subject=ctx.subject,
            roles=ctx.roles,
            identity_kind=ctx.identity_kind,
            agent_scope=ctx.agent_scope,
        )
        outcome = await BudgetService(runtime.context.backend).partial_outcome(
            auth, state['run_id']
        )
        data = outcome.model_dump(mode='json')
        data['summary'] = 'Workflow execution requires review; business outcome is not established.'
        data['unresolved_items'] = ['Workflow execution requires verification.']
        if state.get('missing'):
            data['unresolved_items'].append('Pinned workflow is unavailable.')
        wf = await _load(runtime.context)
        if wf:
            for item in wf.steps:
                result = state.get('results', {}).get(item.step_id, {'status': 'blocked'})
                if item.required and result['status'] not in (
                    ('ok', 'skipped') if version == GraphId.SOP_CONDITIONAL else ('ok',)
                ):
                    data['unresolved_items'].append(item.step_id + ': ' + result['status'])
        return {'outcome': data}

    graph = StateGraph(SopState)
    graph.add_node('prepare', prepare)
    graph.add_node('step', step)
    graph.add_node('compose', compose)
    graph.add_edge(START, 'prepare')
    graph.add_conditional_edges('prepare', route, {'step': 'step', 'compose': 'compose'})
    graph.add_conditional_edges('step', route, {'step': 'step', 'compose': 'compose'})
    graph.add_edge('compose', END)
    return graph.compile(checkpointer=saver)

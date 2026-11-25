"""Pure evidence checks shared by the scoped RCA graphs."""

import json


def evidence_matches(row, scope_id, scope_values):
    return (
        row['scope_id'] == scope_id
        and row['quality'] == 'valid'
        and json.loads(row['time_window'])
        == {key: scope_values[key] for key in ('start_at', 'end_at')}
    )


async def _select_baseline_bindings(execution, uow, wanted):
    from workbench_agent.application.idempotency import digest
    from workbench_agent.tools.policy import load_published_binding

    context = execution.claim.context
    selected = {key: [] for key in wanted}
    for ref in context.tools:
        row = await execution.backend.configuration.one(
            uow,
            'tool_bindings',
            team_id=context.team_id,
            binding_id=ref.resource_id,
            agent_id=context.agent_id,
            version=int(ref.version),
            hash=ref.content_hash,
        )
        if not row:
            continue
        raw = json.loads(row['schema'])
        if digest(raw) != ref.content_hash:
            continue
        binding = load_published_binding(raw)
        if binding and binding.read_only:
            for key, name in wanted.items():
                if binding.tool_name == name:
                    selected[key].append((ref, binding))
    return selected


async def _baseline_plan(execution, state, executor):
    """Map frozen tools against current trusted policy before any dispatch."""
    from workbench_agent.tools.mcp import MCPAdapter, scope_identity

    if not isinstance(executor.transport, MCPAdapter):
        return {}, None, False
    context = execution.claim.context
    wanted = {'metrics': 'metrics_query', 'logs': 'logs_search', 'trace': 'trace_get'}
    async with execution.backend.create_uow().begin_read() as uow:
        current = await execution.backend.runs.current_scope(uow, context.team_id, state['run_id'])
        if not current or current['scope_id'] != state['scope_ref']:
            return {}, None, True
        scope_values = json.loads(current['scope_json'])
        selected = await _select_baseline_bindings(execution, uow, wanted)
    template = state.get('metric_template')
    trace_id = state.get('business_context', {}).get('trace_id')
    arguments = {'metrics': {'template_id': template}, 'logs': {}, 'trace': {'trace_id': trace_id}}
    result = {}
    for key in ('metrics', 'logs', 'trace'):
        if len(selected[key]) != 1:
            continue
        if key == 'metrics' and (
            not isinstance(template, str)
            or not template.strip()
            or template != getattr(execution.backend, '_rca_metric_template', None)
        ):
            continue
        if key == 'trace' and (
            not isinstance(trace_id, str) or not trace_id.strip() or len(trace_id) > 128
        ):
            continue
        ref, binding = selected[key][0]
        try:
            mapped = executor.transport.prepare(
                binding.server,
                binding.credential_scope,
                binding.tool_name,
                arguments[key],
                team_id=context.team_id,
                agent_id=context.agent_id,
                query_scope=scope_values,
                correlation={
                    'run_id': state['run_id'],
                    'invocation_id': 'baseline-scope-check',
                    'query_scope_ref': state['scope_ref'],
                },
            )
            identity = scope_identity(mapped)
        except PermissionError:
            continue
        result[key] = (ref, arguments[key], identity)
    return result, scope_values, False


def _trusted_baseline_executor(execution):
    from workbench_agent.tools.policy import PolicyToolExecutor

    factory = getattr(execution.backend, '_baseline_factory', None)
    if factory is None:
        return None
    try:
        executor = factory(execution)
        if not isinstance(executor, PolicyToolExecutor) or executor.execution is not execution:
            raise ValueError('trusted baseline executor required')
        return executor
    except ValueError:
        return None


async def _baseline_evidence_status(execution, state, scope_values, evidence_ref):
    if not evidence_ref:
        return 'unavailable'
    context = execution.claim.context
    async with execution.backend.create_uow().begin_read() as uow:
        current = await execution.backend.runs.current_scope(uow, context.team_id, state['run_id'])
        rows = await execution.backend.results.evidence(
            uow, context.team_id, state['run_id'], {evidence_ref}
        )
        if not current or current['scope_id'] != state['scope_ref']:
            return 'stale'
        if len(rows) == 1 and evidence_matches(rows[0], state['scope_ref'], scope_values):
            return 'valid'
    return 'unavailable'


async def _collect_one_baseline(
    execution, state, executor, key, entry, authority, scope_values, *, graph_id
):
    from workbench_agent.application.errors import DomainError
    from uuid import UUID, uuid5

    ref, args, _ = entry
    wanted = {'metrics': 'metrics_query', 'logs': 'logs_search', 'trace': 'trace_get'}
    invocation = str(uuid5(UUID(state['run_id']), f'{graph_id}:baseline:' + key))
    try:
        result = await executor.execute(
            execution.claim.context,
            execution.guard,
            invocation,
            ref,
            args,
            expected_tool=wanted[key],
            max_logical_calls=4,
            expected_scope_id=state['scope_ref'],
            expected_business_scope=authority,
        )
    except DomainError as error:
        if error.code == 'BASELINE_SCOPE_CONFLICT':
            return True, None, None
        if error.code == 'STALE_QUERY_SCOPE':
            return False, 'stale', None
        if error.code in ('BUDGET_EXCEEDED', 'EXPERT_TOOL_LIMIT'):
            return False, 'unavailable', None
        raise
    if result.error and result.error.code == 'BASELINE_SCOPE_CONFLICT':
        return True, None, None
    if result.status != 'ok':
        return False, result.status.value, None
    status = await _baseline_evidence_status(execution, state, scope_values, result.evidence_ref)
    return False, status, result.evidence_ref if status == 'valid' else None

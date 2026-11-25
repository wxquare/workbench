"""Pinned v13 synthetic alert scoped, deterministic read-only baseline."""

import json
from typing import TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.runtime import Runtime
from langgraph.types import interrupt

from workbench_agent.application.context import ContextResolver
from workbench_agent.domain.identity import AuthContext
from workbench_agent.domain.query_scope import (
    AlertScopeInput,
    alert_window,
    resolve_alert_scope,
    revise_alert_scope,
)
from workbench_agent.runtime.budgets.service import BudgetService
from workbench_agent.runtime.graphs.ids import GraphId
from workbench_agent.runtime.graphs.rca_baseline import (
    _baseline_plan,
    _collect_one_baseline,
    _trusted_baseline_executor,
)
from workbench_agent.runtime.graphs.interrupts import INPUT_SCHEMA

STATE_SCHEMA_VERSION = 'dod-state-v13'


class RcaState(TypedDict, total=False):
    run_id: str
    actor: str
    mode: str
    business_context: dict
    phase: str
    region: str
    last_applied_resume_id: str
    outcome: dict
    scope_unavailable: bool
    initial_scope_id: str
    scope_ref: str
    metric_template: str | None
    baseline_statuses: dict
    baseline_refs: list[str]
    baseline_authority: list[str]
    baseline_eligible: list[str]
    baseline_conflict: bool


def _alert(state, execution):
    context = state.get('business_context', {})
    return AlertScopeInput(
        team=execution.claim.context.team_id,
        service=context['service'],
        environment=context['environment'],
        body_region=context.get('body_region', context.get('region')),
        link_region=context.get('link_region'),
        anchor=context['anchor'],
        window_seconds=context['window_seconds'],
    )


async def prepare(state, config, runtime: Runtime):
    await runtime.context.check_operation()
    if state['run_id'] != config['configurable']['thread_id']:
        raise ValueError('thread_id must equal run_id')
    execution = runtime.context
    if state.get('mode') != 'rca':
        return {'phase': 'prepare', 'scope_unavailable': True}
    try:
        alert = _alert(state, execution)
        start, end = alert_window(alert)
    except (KeyError, TypeError, ValueError):
        return {'phase': 'prepare', 'scope_unavailable': True}
    values = {
        'service': alert.service,
        'environment': alert.environment,
        'start_at': start.isoformat(),
        'end_at': end.isoformat(),
    }
    context = execution.claim.context
    auth = AuthContext(
        team_id=context.team_id,
        subject=context.subject,
        roles=context.roles,
        identity_kind=context.identity_kind,
        agent_scope=context.agent_scope,
    )
    async with execution.backend.create_uow().begin_write() as uow:
        await execution.backend.lease_guard.require(uow, execution.guard)
        current = await execution.backend.runs.current_scope(uow, context.team_id, state['run_id'])
        if current and json.loads(current['scope_json']) != values:
            return {'phase': 'prepare', 'scope_unavailable': True}
        revision = await ContextResolver(execution.backend).query_scope(
            uow, auth, state['run_id'], values
        )
    return {
        'phase': 'prepare',
        'initial_scope_id': revision['scope_id'],
        'metric_template': getattr(execution.backend, '_rca_metric_template', None),
    }


async def input_node(state, runtime: Runtime):
    await runtime.context.check_operation()
    if state.get('scope_unavailable'):
        return {'phase': 'input', 'scope_unavailable': True}
    try:
        resolved = resolve_alert_scope(_alert(state, runtime.context))
    except (KeyError, TypeError, ValueError):
        return {'phase': 'input', 'scope_unavailable': True}
    if resolved.scope is not None:
        return {'phase': 'input', 'region': resolved.scope.region}
    response = interrupt(
        {
            'name': 'foundation.input',
            'kind': 'user_input',
            'actor': state['actor'],
            'schema': INPUT_SCHEMA,
            'expected_scope_id': state['initial_scope_id'],
        }
    )
    return {
        'phase': 'input',
        'region': response['response']['values']['region'],
        'last_applied_resume_id': response['resume_request_id'],
    }


async def scope(state, runtime: Runtime):
    execution = runtime.context
    await execution.check_operation()
    if state.get('scope_unavailable'):
        return {'phase': 'scope'}
    try:
        value = revise_alert_scope(_alert(state, execution), {'region': state['region']})
    except (KeyError, TypeError, ValueError):
        return {'phase': 'scope', 'scope_unavailable': True}
    values = {
        'region': value.region,
        'service': value.service,
        'environment': value.environment,
        'start_at': value.start.isoformat(),
        'end_at': value.end.isoformat(),
    }
    context = execution.claim.context
    auth = AuthContext(
        team_id=context.team_id,
        subject=context.subject,
        roles=context.roles,
        identity_kind=context.identity_kind,
        agent_scope=context.agent_scope,
    )
    async with execution.backend.create_uow().begin_write() as uow:
        await execution.backend.lease_guard.require(uow, execution.guard)
        current = await execution.backend.runs.current_scope(uow, context.team_id, state['run_id'])
        # Resume atomically merges the confirmed region into the bound scope.
        # Never replace an unrelated scope from another authorized operation.
        base = {key: val for key, val in values.items() if key != 'region'}
        if not current or not (
            (
                current['scope_id'] == state['initial_scope_id']
                and json.loads(current['scope_json']) == base
            )
            or json.loads(current['scope_json']) == values
        ):
            return {'phase': 'scope', 'scope_unavailable': True}
        revision = await ContextResolver(execution.backend).query_scope(
            uow, auth, state['run_id'], values
        )
    return {'phase': 'scope', 'scope_ref': revision['scope_id']}


async def bind_baseline(state, runtime: Runtime):
    execution = runtime.context
    await execution.check_operation()
    if state.get('scope_unavailable') or not state.get('scope_ref'):
        return {'phase': 'bind_baseline', 'baseline_eligible': []}
    executor = _trusted_baseline_executor(execution)
    if executor is None:
        return {'phase': 'bind_baseline', 'baseline_eligible': []}
    plan, _, stale = await _baseline_plan(execution, state, executor)
    identities = {entry[2] for entry in plan.values()}
    if stale or len(identities) > 1:
        return {'phase': 'bind_baseline', 'baseline_eligible': [], 'baseline_conflict': True}
    return {
        'phase': 'bind_baseline',
        'baseline_eligible': list(plan),
        'baseline_authority': list(next(iter(identities))) if identities else [],
    }


async def collect_baseline(state, runtime: Runtime):
    execution = runtime.context
    await execution.check_operation()
    statuses = {name: 'unavailable' for name in ('metrics', 'logs', 'trace')}
    refs = []
    if state.get('baseline_conflict'):
        return {
            'phase': 'collect_baseline',
            'baseline_statuses': {k: 'conflicting' for k in statuses},
        }
    eligible = set(state.get('baseline_eligible', []))
    authority = tuple(state.get('baseline_authority', ()))
    if not eligible or not authority:
        return {'phase': 'collect_baseline', 'baseline_statuses': statuses}
    executor = _trusted_baseline_executor(execution)
    if executor is None:
        return {'phase': 'collect_baseline', 'baseline_statuses': statuses}
    plan, scope_values, stale = await _baseline_plan(execution, state, executor)
    if stale:
        return {
            'phase': 'collect_baseline',
            'baseline_statuses': {k: 'stale' for k in statuses},
        }
    if any(key in plan and plan[key][2] != authority for key in eligible):
        return {
            'phase': 'collect_baseline',
            'baseline_statuses': {k: 'conflicting' for k in statuses},
        }
    for key in ('metrics', 'logs', 'trace'):
        if key not in eligible or key not in plan:
            continue
        conflict, status, evidence_ref = await _collect_one_baseline(
            execution,
            state,
            executor,
            key,
            plan[key],
            authority,
            scope_values,
            graph_id=GraphId.RCA_SYNTHETIC,
        )
        if conflict:
            return {
                'phase': 'collect_baseline',
                'baseline_statuses': {k: 'conflicting' for k in statuses},
            }
        statuses[key] = status
        if evidence_ref:
            refs.append(evidence_ref)
    return {'phase': 'collect_baseline', 'baseline_statuses': statuses, 'baseline_refs': refs}


async def compose(state, runtime: Runtime):
    await runtime.context.check_operation()
    context = runtime.context.claim.context
    auth = AuthContext(
        team_id=context.team_id,
        subject=context.subject,
        roles=context.roles,
        identity_kind=context.identity_kind,
        agent_scope=context.agent_scope,
    )
    outcome = await BudgetService(runtime.context.backend).partial_outcome(auth, state['run_id'])
    data = outcome.model_dump(mode='json')
    data['summary'] = (
        'Alert analysis is incomplete; baseline observations do not establish a root cause or normal business health.'
    )
    data['evidence_refs'] = state.get('baseline_refs', [])
    data['unresolved_items'] = (
        ['A complete, valid alert scope is required.']
        if state.get('scope_unavailable')
        else ['Root cause and business health are not established by baseline retrieval alone.']
    )
    for name, status in state.get('baseline_statuses', {}).items():
        if name in ('metrics', 'logs', 'trace') and status in (
            'empty',
            'unavailable',
            'timeout',
            'denied',
            'invalid_input',
            'unknown',
            'stale',
            'conflicting',
        ):
            data['unresolved_items'].append(f'{name} baseline: {status}.')
    return {'phase': 'compose', 'outcome': data}


def build_rca_synthetic(saver):
    graph = StateGraph(RcaState)
    graph.add_node('prepare', prepare)
    graph.add_node('input', input_node)
    graph.add_node('scope', scope)
    graph.add_node('bind_baseline', bind_baseline)
    graph.add_node('collect_baseline', collect_baseline)
    graph.add_node('compose', compose)
    graph.add_edge(START, 'prepare')
    graph.add_edge('prepare', 'input')
    graph.add_edge('input', 'scope')
    graph.add_edge('scope', 'bind_baseline')
    graph.add_edge('bind_baseline', 'collect_baseline')
    graph.add_edge('collect_baseline', 'compose')
    graph.add_edge('compose', END)
    return graph.compile(checkpointer=saver)

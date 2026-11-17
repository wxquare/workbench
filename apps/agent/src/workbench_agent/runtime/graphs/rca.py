"""Pinned v9 scope-resolution slice; no baseline is claimed without tools."""
import json
from typing import TypedDict
from langgraph.graph import StateGraph, START, END
from langgraph.runtime import Runtime
from langgraph.types import interrupt

from workbench_agent.application.context import ContextResolver
from workbench_agent.domain.identity import AuthContext
from workbench_agent.domain.query_scope import AlertScopeInput, alert_window, resolve_alert_scope, revise_alert_scope
from workbench_agent.runtime.budgets.service import BudgetService
from workbench_agent.runtime.graphs.interrupts import INPUT_SCHEMA

STATE_SCHEMA_VERSION = 'dod-state-v9'


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


def _alert(state, execution):
    context = state.get('business_context', {})
    return AlertScopeInput(team=execution.claim.context.team_id,
        service=context['service'], environment=context['environment'],
        body_region=context.get('body_region', context.get('region')),
        link_region=context.get('link_region'), anchor=context['anchor'],
        window_seconds=context['window_seconds'])


def build_rca(saver):
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
        values = {'service': alert.service, 'environment': alert.environment,
                  'start_at': start.isoformat(), 'end_at': end.isoformat()}
        context = execution.claim.context
        auth = AuthContext(team_id=context.team_id, subject=context.subject, roles=context.roles,
                           identity_kind=context.identity_kind, agent_scope=context.agent_scope)
        async with execution.backend.create_uow().begin_write() as uow:
            await execution.backend.lease_guard.require(uow, execution.guard)
            current = await execution.backend.runs.current_scope(uow, context.team_id, state['run_id'])
            if current and json.loads(current['scope_json']) != values:
                return {'phase': 'prepare', 'scope_unavailable': True}
            revision = await ContextResolver(execution.backend).query_scope(uow, auth, state['run_id'], values)
        return {'phase': 'prepare', 'initial_scope_id': revision['scope_id']}

    async def input(state, runtime: Runtime):
        await runtime.context.check_operation()
        if state.get('scope_unavailable'):
            return {'phase': 'input', 'scope_unavailable': True}
        try:
            resolved = resolve_alert_scope(_alert(state, runtime.context))
        except (KeyError, TypeError, ValueError):
            return {'phase': 'input', 'scope_unavailable': True}
        if resolved.scope is not None:
            return {'phase': 'input', 'region': resolved.scope.region}
        response = interrupt({'name': 'foundation.input', 'kind': 'user_input',
            'actor': state['actor'], 'schema': INPUT_SCHEMA,
            'expected_scope_id': state['initial_scope_id']})
        return {'phase': 'input', 'region': response['response']['values']['region'],
                'last_applied_resume_id': response['resume_request_id']}

    async def scope(state, runtime: Runtime):
        execution = runtime.context
        await execution.check_operation()
        if state.get('scope_unavailable'):
            return {'phase': 'scope'}
        try:
            value = revise_alert_scope(_alert(state, execution), {'region': state['region']})
        except (KeyError, TypeError, ValueError):
            return {'phase': 'scope', 'scope_unavailable': True}
        values = {'region': value.region, 'service': value.service,
                  'environment': value.environment, 'start_at': value.start.isoformat(),
                  'end_at': value.end.isoformat()}
        context = execution.claim.context
        auth = AuthContext(team_id=context.team_id, subject=context.subject, roles=context.roles,
                           identity_kind=context.identity_kind, agent_scope=context.agent_scope)
        async with execution.backend.create_uow().begin_write() as uow:
            await execution.backend.lease_guard.require(uow, execution.guard)
            current = await execution.backend.runs.current_scope(uow, context.team_id, state['run_id'])
            # Resume atomically merges the confirmed region into the bound scope.
            # Never replace an unrelated scope from another authorized operation.
            base = {key: val for key, val in values.items() if key != 'region'}
            if not current or not ((current['scope_id'] == state['initial_scope_id']
                    and json.loads(current['scope_json']) == base)
                    or json.loads(current['scope_json']) == values):
                return {'phase': 'scope', 'scope_unavailable': True}
            await ContextResolver(execution.backend).query_scope(uow, auth, state['run_id'], values)
        return {'phase': 'scope'}

    async def compose(state, runtime: Runtime):
        await runtime.context.check_operation()
        context = runtime.context.claim.context
        auth = AuthContext(team_id=context.team_id, subject=context.subject, roles=context.roles,
                           identity_kind=context.identity_kind, agent_scope=context.agent_scope)
        outcome = await BudgetService(runtime.context.backend).partial_outcome(auth, state['run_id'])
        data = outcome.model_dump(mode='json')
        data['summary'] = 'Alert analysis is incomplete; no baseline has been collected.'
        data['unresolved_items'] = (['A complete, valid alert scope is required.'] if state.get('scope_unavailable')
                                    else ['Required monitoring, log and trace baseline is unavailable.'])
        return {'phase': 'compose', 'outcome': data}

    graph = StateGraph(RcaState)
    for node in (prepare, input, scope, compose):
        graph.add_node(node.__name__, node)
    graph.add_edge(START, 'prepare'); graph.add_edge('prepare', 'input')
    graph.add_edge('input', 'scope'); graph.add_edge('scope', 'compose'); graph.add_edge('compose', END)
    return graph.compile(checkpointer=saver)

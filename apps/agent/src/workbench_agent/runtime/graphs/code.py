"""Pinned M2 graph: bounded, checkpointed progress with conservative fallback.

External expert capabilities are not yet configured. Until they are available,
this graph produces an explicit partial rather than inventing business evidence.
"""
from dataclasses import asdict
from typing import Annotated, TypedDict

from langchain_core.messages import AnyMessage, HumanMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.runtime import Runtime

from workbench_agent.domain.identity import AuthContext
from workbench_agent.runtime.budgets.service import BudgetService

STATE_SCHEMA_VERSION = 'dod-state-v6'


class DoDState(TypedDict, total=False):
    run_id: str
    input_text: str
    mode: str
    route: str
    phase: str
    messages: Annotated[list[AnyMessage], add_messages]
    unresolved: list[str]
    outcome: dict
    validation: dict
    evidence_refs: list[str]
    diagnosis_done: bool


def build_dod(saver):
    async def prepare_context(state, config, runtime: Runtime):
        await runtime.context.check_operation()
        if state['run_id'] != config['configurable']['thread_id']:
            raise ValueError('graph thread_id must equal run_id')
        return {'phase': 'prepare_context', 'messages': [HumanMessage(state['input_text'], id=state['run_id'] + ':input')]}

    async def route(state, runtime: Runtime):
        await runtime.context.check_operation()
        mode = state['mode']
        # No model classifier is enabled: ambiguous input stays unresolved.
        return {'phase': 'route', 'route': mode}

    async def select_skills(state, runtime: Runtime):
        await runtime.context.check_operation()
        return {'phase': 'select_skills'}

    async def collect_baseline(state, runtime: Runtime):
        await runtime.context.check_operation()
        return {'phase': 'collect_baseline'}

    async def investigate(state, runtime: Runtime):
        await runtime.context.check_operation()
        execution = runtime.context
        factory = getattr(execution.backend, '_code_factory', None)
        context = execution.claim.context
        if state['route'] != 'code' or factory is None or len(context.repositories) != 1:
            return {'phase': 'investigate', 'unresolved': ['A single pinned repository and code capability are required.'],
                    'evidence_refs': [], 'diagnosis_done': False}
        from workbench_agent.runtime.experts.code import CodeExpert, ExpertTask
        from workbench_agent.application.errors import DomainError
        from workbench_agent.jobs.worker import CapabilityBlocked
        try:
            expert = CodeExpert(execution, factory(execution), saver=saver, guard_provider=saver.guard_provider)
            result = await expert.invoke(ExpertTask(task_id='baseline', instruction=state['input_text']))
        except DomainError as error:
            if error.code not in ('BUDGET_EXCEEDED', 'MAX_CONTEXT_EXCEEDED', 'SUMMARY_RESERVE_REQUIRED', 'MODEL_PROFILE_UNAVAILABLE', 'INVOCATION_ALREADY_SENT', 'INVOCATION_ALREADY_STARTED', 'RESERVATION_CONFLICT', 'STALE_QUERY_SCOPE', 'FORBIDDEN', 'SNAPSHOT_UNAVAILABLE', 'QUERY_SCOPE_REQUIRED', 'EXPERT_TOOL_LIMIT'):
                raise
            return {'phase': 'investigate', 'unresolved': ['Code evidence could not be collected within current scope, permissions, and budget.'],
                    'evidence_refs': [], 'diagnosis_done': False}
        except (CapabilityBlocked, ValueError):
            return {'phase': 'investigate', 'unresolved': ['Code capability or requested snapshot lines are unavailable.'],
                    'evidence_refs': [], 'diagnosis_done': False}
        return {'phase': 'investigate', 'unresolved': ['Code evidence requires verification before a definitive answer.'],
                'evidence_refs': list(result.evidence_refs), 'diagnosis_done': True}

    async def validate_evidence(state, runtime: Runtime):
        await runtime.context.check_operation()
        from workbench_agent.runtime.graphs.validation import EvidenceValidator
        async with runtime.context.backend.create_uow().begin_read() as uow:
            scope = await runtime.context.backend.runs.current_scope(
                uow, runtime.context.guard.team_id, state['run_id'])
            rows = await runtime.context.backend.results.evidence(uow, runtime.context.guard.team_id,
                state['run_id'], set(state.get('evidence_refs', []))) if state.get('evidence_refs') else []
            evidence = {}
            for row in rows:
                value = dict(row)
                payload = await runtime.context.backend.tools.evidence_payload(uow, runtime.context.guard.team_id,
                    state['run_id'], value['evidence_id'])
                value['reference_accessible'] = bool(payload and not payload['truncated'])
                evidence[value['evidence_id']] = value
        completed = {'investigate'} if state.get('diagnosis_done') else set()
        result = EvidenceValidator().validate(scope['scope_id'] if scope else None, evidence,
            {'investigate'}, completed_steps=completed, referenced_ids=set(state.get('evidence_refs', [])))
        return {'phase': 'validate_evidence', 'validation': asdict(result)}

    async def compose_outcome(state, runtime: Runtime):
        await runtime.context.check_operation()
        context = runtime.context.claim.context
        auth = AuthContext(team_id=context.team_id, subject=context.subject, roles=context.roles,
                           identity_kind=context.identity_kind, agent_scope=context.agent_scope)
        outcome = await BudgetService(runtime.context.backend).partial_outcome(auth, state['run_id'])
        data = outcome.model_dump(mode='json')
        data['summary'] = ('Pinned repository evidence was collected; the analysis remains incomplete.'
                           if state['validation']['valid_refs'] else
                           'Analysis is incomplete because required expert execution and evidence are unavailable.')
        data['unresolved_items'] = state['unresolved']
        data['evidence_refs'] = list(state['validation']['valid_refs'])
        return {'phase': 'compose_outcome', 'outcome': data}

    async def finalize(state, runtime: Runtime):
        await runtime.context.check_operation()
        return {'phase': 'finalize'}

    graph = StateGraph(DoDState)
    nodes = (prepare_context, route, select_skills, collect_baseline, investigate,
             validate_evidence, compose_outcome, finalize)
    for node in nodes:
        graph.add_node(node.__name__, node)
    graph.add_edge(START, nodes[0].__name__)
    for left, right in zip(nodes, nodes[1:]):
        graph.add_edge(left.__name__, right.__name__)
    graph.add_edge(nodes[-1].__name__, END)
    return graph.compile(checkpointer=saver)

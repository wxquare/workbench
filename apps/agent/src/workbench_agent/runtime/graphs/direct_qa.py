"""Pinned, tool-free model answers, without a claim of external verification."""
from typing import TypedDict
from uuid import UUID, uuid5

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime

from workbench_agent.application.errors import DomainError
from workbench_agent.domain.identity import AuthContext
from workbench_agent.jobs.worker import CapabilityBlocked
from workbench_agent.runtime.budgets.service import BudgetService
from workbench_agent.runtime.graphs.ids import GraphId
from workbench_agent.runtime.graphs.agent_instructions import system_instruction

STATE_SCHEMA_VERSION = 'dod-state-v17'


class DirectQAState(TypedDict, total=False):
    run_id: str
    input_text: str
    outcome: dict
    phase: str


def build_direct_qa(saver):
    async def answer(state, config, runtime: Runtime):
        execution = runtime.context
        await execution.check_operation()
        if state['run_id'] != config['configurable']['thread_id']:
            raise ValueError('graph thread_id must equal run_id')
        factory = getattr(execution.backend, '_code_factory', None)
        text, error = None, 'MODEL_CAPABILITY_UNAVAILABLE'
        if factory is not None:
            try:
                result = await factory(execution).invoke(execution.guard, 'summary', [
                    SystemMessage(content=system_instruction(execution.claim.context)),
                    HumanMessage(content=state['input_text']),
                ], logical_call_id=str(uuid5(UUID(state['run_id']), f'{GraphId.CONVERSATION_SINGLE_TURN}:answer')))
                if result.status == 'ok' and result.text and len(result.text) <= 65500:
                    text = result.text
                else:
                    error = result.error_code or 'MODEL_OUTPUT_UNAVAILABLE'
            except DomainError as exc:
                if exc.code not in ('BUDGET_EXCEEDED', 'SUMMARY_RESERVE_REQUIRED', 'MAX_CONTEXT_EXCEEDED',
                        'MODEL_PROFILE_UNAVAILABLE', 'INVOCATION_ALREADY_SENT', 'RESERVATION_CONFLICT',
                        'MODEL_CREDENTIAL_UNAVAILABLE'):
                    raise
                error = exc.code
            except CapabilityBlocked:
                pass
        await execution.check_operation()
        context = execution.claim.context
        auth = AuthContext(team_id=context.team_id, subject=context.subject, roles=context.roles,
            identity_kind=context.identity_kind, agent_scope=context.agent_scope)
        outcome = (await BudgetService(execution.backend).partial_outcome(auth, state['run_id'])).model_dump(mode='json')
        if text is not None:
            outcome.update(completed=True, summary='以下内容由模型生成，未经外部知识源验证。\n\n' + text, unresolved_items=[])
        else:
            outcome.update(summary='Model answer unavailable.', unresolved_items=[f'Model invocation did not complete: {error}.'])
        return {'phase': 'answer', 'outcome': outcome}

    graph = StateGraph(DirectQAState)
    graph.add_node('answer', answer)
    graph.add_edge(START, 'answer')
    graph.add_edge('answer', END)
    return graph.compile(checkpointer=saver)

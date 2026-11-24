"""Test-only input and approval graph for durable resume boundaries.

Sync durability on resumed invocation is mandatory: no downstream operation can
start until the input node's last_applied_resume_id checkpoint commits.
"""

from typing import TypedDict, Annotated
from langchain_core.messages import AnyMessage, AIMessage, HumanMessage
from langgraph.graph.message import add_messages
from langgraph.graph import StateGraph, START, END
from langgraph.runtime import Runtime
from langgraph.types import interrupt

STATE_SCHEMA_VERSION = 'dod-state-v2'
INPUT_SCHEMA = {
    'type': 'object',
    'properties': {'region': {'type': 'string'}},
    'required': ['region'],
    'additionalProperties': False,
}


class InterruptState(TypedDict, total=False):
    run_id: str
    input_text: str
    actor: str
    requires_input: bool
    requires_approval: bool
    args_hash: str
    messages: Annotated[list[AnyMessage], add_messages]
    progress: str
    last_applied_resume_id: str
    input_values: dict
    approval_decision: str
    final_progress: dict
    outcome: dict


def build_interrupt_graph(saver):
    async def prepare(state, config, runtime: Runtime):
        await runtime.context.check_operation()
        if state['run_id'] != config['configurable']['thread_id']:
            raise ValueError('thread_id must equal run_id')
        return {
            'progress': 'prepared',
            'messages': [HumanMessage(state['input_text'], id=state['run_id'] + ':input')],
        }

    async def accept(state, runtime: Runtime):
        await runtime.context.check_operation()
        if state.get('requires_approval'):
            descriptor = {
                'name': 'foundation.approval',
                'kind': 'approval',
                'actor': state['actor'],
                'approval_id': state['run_id'] + ':approval',
                'args_hash': state['args_hash'],
                'schema': {'type': 'approval-v1'},
            }
        elif state.get('requires_input'):
            descriptor = {
                'name': 'foundation.input',
                'kind': 'user_input',
                'actor': state['actor'],
                'schema': INPUT_SCHEMA,
            }
        else:
            return {'progress': 'input_received'}
        value = interrupt(descriptor)
        # Only canonical validated ResumeRequest envelopes may cross this node.
        request_id = value['resume_request_id']
        response = value['response']
        result = {'last_applied_resume_id': request_id, 'progress': 'input_received'}
        if response['kind'] == 'approval':
            result['approval_decision'] = response['decision']
        else:
            result.update(
                input_values=response['values'],
                input_text=response.get('text') or response['values']['region'],
            )
        return result

    async def compose(state, runtime: Runtime):
        await runtime.context.check_operation()
        summary = 'Foundation input recorded.'
        descriptor = {
            'kind': 'foundation.recorded.v1',
            'completed': True,
            'summary': summary,
            'input_text': state['input_text'],
            'input_values': state.get('input_values', {}),
            'approval_decision': state.get('approval_decision'),
            'last_applied_resume_id': state.get('last_applied_resume_id'),
        }
        outcome = {
            'kind': 'foundation.recorded.v1',
            'completed': True,
            'summary': 'Foundation input recorded. No business diagnosis or model inference was performed.',
            'findings': [],
            'root_cause_hypotheses': [],
            'impact': None,
            'evidence_refs': [],
            'unresolved_items': [],
            'proposed_actions': [],
            'knowledge_gaps': [],
            'usage_summary': {
                'status': 'complete',
                'input_tokens': 0,
                'output_tokens': 0,
                'amount_micros': 0,
                'currency': 'USD',
            },
        }
        return {
            'progress': 'composed',
            'final_progress': descriptor,
            'outcome': outcome,
            'messages': [AIMessage(summary, id=state['run_id'] + ':foundation')],
        }

    graph = StateGraph(InterruptState)
    graph.add_node('prepare', prepare)
    graph.add_node('input', accept)
    graph.add_node('compose', compose)
    graph.add_edge(START, 'prepare')
    graph.add_edge('prepare', 'input')
    graph.add_edge('input', 'compose')
    graph.add_edge('compose', END)
    return graph.compile(checkpointer=saver)

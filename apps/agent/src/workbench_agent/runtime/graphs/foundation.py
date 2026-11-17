"""M1 foundation only: deterministic checkpointable progress, no business Outcome."""
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import StateGraph, START, END
from langgraph.runtime import Runtime
from langgraph.types import interrupt
from workbench_agent.runtime.state.schema import FoundationState


def build_foundation(saver):
    async def prepare(state,config,runtime:Runtime):
        await runtime.context.check_operation()
        if state['run_id']!=config['configurable']['thread_id']:
            raise ValueError('graph thread_id must equal run_id')
        return {'progress':'prepared','messages':[HumanMessage(state['input_text'],id=state['run_id']+':input')]}

    async def accept_input(state,runtime:Runtime):
        await runtime.context.check_operation()
        if state.get('requires_input'):
            answer=interrupt({'kind':'input','prompt':'Additional input required'})
            return {'input_text':answer,'progress':'input_received'}
        return {'progress':'input_received'}

    async def compose(state,runtime:Runtime):
        await runtime.context.check_operation()
        return {'progress':'composed','messages':[AIMessage('Foundation input recorded.',id=state['run_id']+':foundation')]}

    graph=StateGraph(FoundationState)
    graph.add_node('prepare',prepare)
    graph.add_node('input',accept_input)
    graph.add_node('compose',compose)
    graph.add_edge(START,'prepare'); graph.add_edge('prepare','input')
    graph.add_edge('input','compose'); graph.add_edge('compose',END)
    return graph.compile(checkpointer=saver)

"""Immutable dod.v2: input-only resume boundary, durable marker, then compose.

Sync durability on resumed invocation is mandatory: no downstream operation can
start until the input node's last_applied_resume_id checkpoint commits.
"""
from typing import TypedDict, Annotated
from langchain_core.messages import AnyMessage, AIMessage, HumanMessage
from langgraph.graph.message import add_messages
from langgraph.graph import StateGraph, START, END
from langgraph.runtime import Runtime
from langgraph.types import interrupt

STATE_SCHEMA_VERSION='dod-state-v2'
INPUT_SCHEMA={'type':'object','properties':{'region':{'type':'string'}},'required':['region'],'additionalProperties':False}
class InterruptState(TypedDict,total=False):
    run_id:str
    input_text:str
    actor:str
    requires_input:bool
    requires_approval:bool
    args_hash:str
    messages:Annotated[list[AnyMessage],add_messages]
    progress:str
    last_applied_resume_id:str
    input_values:dict
    approval_decision:str
    final_progress:dict

def build_interrupt_graph(saver):
    async def prepare(state,config,runtime:Runtime):
        await runtime.context.check_operation()
        if state['run_id']!=config['configurable']['thread_id']: raise ValueError('thread_id must equal run_id')
        return {'progress':'prepared','messages':[HumanMessage(state['input_text'],id=state['run_id']+':input')]}
    async def accept(state,runtime:Runtime):
        await runtime.context.check_operation()
        if state.get('requires_approval'):
            descriptor={'name':'foundation.approval','kind':'approval','actor':state['actor'],
                'approval_id':state['run_id']+':approval','args_hash':state['args_hash'],'schema':{'type':'approval-v1'}}
        elif state.get('requires_input'):
            descriptor={'name':'foundation.input','kind':'user_input','actor':state['actor'],'schema':INPUT_SCHEMA}
        else: return {'progress':'input_received'}
        value=interrupt(descriptor)
        # Only canonical validated ResumeRequest envelopes may cross this node.
        request_id=value['resume_request_id']; response=value['response']
        result={'last_applied_resume_id':request_id,'progress':'input_received'}
        if response['kind']=='approval': result['approval_decision']=response['decision']
        else:
            result.update(input_values=response['values'],input_text=response.get('text') or response['values']['region'])
        return result
    async def compose(state,runtime:Runtime):
        await runtime.context.check_operation()
        summary='Foundation input recorded.'
        descriptor={'kind':'foundation.recorded.v1','completed':True,'summary':summary,
            'input_text':state['input_text'],'input_values':state.get('input_values',{}),
            'approval_decision':state.get('approval_decision'),'last_applied_resume_id':state.get('last_applied_resume_id')}
        return {'progress':'composed','final_progress':descriptor,
            'messages':[AIMessage(summary,id=state['run_id']+':foundation')]}
    graph=StateGraph(InterruptState)
    graph.add_node('prepare',prepare);graph.add_node('input',accept);graph.add_node('compose',compose)
    graph.add_edge(START,'prepare');graph.add_edge('prepare','input');graph.add_edge('input','compose');graph.add_edge('compose',END)
    return graph.compile(checkpointer=saver)

def validate_response(descriptor,response):
    """Exact registered v2 response schemas; unknown schema versions fail closed."""
    if descriptor['kind']!=response['kind']: return False
    if response['kind']=='approval': return descriptor['schema']=={'type':'approval-v1'}
    values=response['values']
    return descriptor['schema']==INPUT_SCHEMA and set(values)=={'region'} and isinstance(values['region'],str) and bool(values['region'])

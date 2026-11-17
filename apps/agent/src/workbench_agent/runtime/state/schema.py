"""M1 deterministic foundation state, pinned independently from graph version."""
from typing import Annotated, TypedDict
from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

STATE_SCHEMA_VERSION='dod-state-v1'

class FoundationState(TypedDict,total=False):
    run_id:str
    input_text:str
    requires_input:bool
    messages:Annotated[list[AnyMessage],add_messages]
    progress:str

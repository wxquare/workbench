"""Frozen tool contracts. Bindings refer only to deployment-owned connections."""
from enum import StrEnum
from typing import Any
from pydantic import Field
from workbench_agent.domain.profiles import FrozenModel, VersionRef

class ToolStatus(StrEnum):
    OK='ok'
    EMPTY='empty'
    UNAVAILABLE='unavailable'
    DENIED='denied'
    INVALID_INPUT='invalid_input'
    TIMEOUT='timeout'
    UNKNOWN='unknown'

class ToolBinding(FrozenModel):
    alias: str = Field(min_length=1,max_length=128)
    provider: str = Field(min_length=1,max_length=128)
    tool_name: str = Field(min_length=1,max_length=128)
    server: str = Field(min_length=1,max_length=128)
    credential_scope: str = Field(min_length=1,max_length=128)
    permission: str = Field(min_length=1,max_length=128)
    input_schema: dict[str,Any]
    output_schema: dict[str,Any] | None = None
    read_only: bool = True
    timeout_seconds: float = Field(default=10,gt=0,le=60)
    max_output_bytes: int = Field(default=65536,ge=64,le=1048576)

class ToolError(FrozenModel):
    code: str
    retryable: bool = False

class ActionProposal(FrozenModel):
    binding_ref: VersionRef
    scope_id: str
    args: dict[str,Any]
    args_hash: str
    status: str = 'proposed'

class ToolResult(FrozenModel):
    status: ToolStatus
    data: Any = None
    error: ToolError | None = None
    evidence_ref: str | None = None
    proposed_action: ActionProposal | None = None

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class DTO(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class ScopeDTO(DTO):
    team: str = Field(min_length=1, max_length=128)
    environment: str = Field(min_length=1, max_length=128)
    region: str = Field(min_length=1, max_length=128)
    service: str = Field(min_length=1, max_length=128)
    source_id: str = Field(min_length=1, max_length=128)


class ContextDTO(DTO):
    run_id: str = Field(min_length=1, max_length=128)
    invocation_id: str = Field(min_length=1, max_length=128)
    query_scope_ref: str = Field(min_length=1, max_length=128)


class ToolResultDTO(DTO):
    schema_version: Literal["dod-tool-result-v1"]
    status: Literal["ok", "empty", "unavailable", "denied", "invalid_input", "timeout", "unknown"]
    data: dict[str, Any]
    evidence_refs: list[dict[str, Any]]
    artifacts: list[Any]
    error: dict[str, str] | None
    retryable: bool
    meta: dict[str, Any]

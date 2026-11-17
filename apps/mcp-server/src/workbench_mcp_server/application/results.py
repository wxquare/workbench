from dataclasses import dataclass, field
from typing import Any

from workbench_mcp_server.domain.evidence import Evidence


class GatewayError(Exception):
    def __init__(self, status: str, code: str, retryable: bool = False):
        super().__init__(code)
        self.status = status
        self.code = code
        self.retryable = retryable


@dataclass
class QueryOutcome:
    status: str
    data: dict[str, Any] = field(default_factory=dict)
    evidence_refs: list[Evidence] = field(default_factory=list)
    error: dict[str, str] | None = None
    retryable: bool = False
    meta: dict[str, Any] = field(default_factory=dict)

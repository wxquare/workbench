from datetime import datetime
from typing import Any, Protocol

from workbench_mcp_server.domain.identity import Principal

from .queries import QueryRequest


class KnowledgeGateway(Protocol):
    async def query(self, request: QueryRequest) -> dict[str, Any]: ...


class MetricsGateway(KnowledgeGateway, Protocol): ...


class LogsGateway(KnowledgeGateway, Protocol): ...


class TraceGateway(KnowledgeGateway, Protocol): ...


class Clock(Protocol):
    def now(self) -> datetime: ...


class AuditSink(Protocol):
    def emit(self, event: dict[str, Any]) -> None: ...


class SecretProvider(Protocol):
    def resolve(self, ref: str) -> str: ...


class Authenticator(Protocol):
    def authenticate(self, token: str) -> Principal | None: ...


class CursorCodec(Protocol):
    def encode(self, principal_id: str, request: QueryRequest, upstream_cursor: str) -> str: ...
    def decode(self, principal_id: str, request: QueryRequest, token: str) -> str: ...


class QueryLimits(Protocol):
    deadline_seconds: float
    global_concurrency: int
    principal_concurrency: int
    window_seconds: int

import asyncio
import hashlib
import json
import time
from dataclasses import asdict, replace
from uuid import uuid4

from workbench_mcp_server.domain.evidence import Evidence
from workbench_mcp_server.domain.policy import ScopePolicy

from ..ports import AuditSink, Clock, CursorCodec, KnowledgeGateway, QueryLimits
from ..queries import RECORD_KEYS, TOOL_KINDS, QueryRequest, RuntimeSource
from ..results import GatewayError, QueryOutcome


class QueryExecutor:
    """Shared authorization/execution boundary for the four read-only queries."""

    def __init__(
        self,
        sources: dict[str, RuntimeSource],
        gateways: dict[str, KnowledgeGateway],
        limits: QueryLimits,
        clock: Clock,
        audit: AuditSink,
        cursor: CursorCodec | None = None,
    ):
        self.sources, self.gateways = sources, gateways
        self.limits, self.clock, self.audit = limits, clock, audit
        self.deadline_seconds = limits.deadline_seconds
        self.cursor = cursor
        self._active = 0
        self._by_principal = {}

    async def execute(self, principal, raw):
        started = time.monotonic()
        request = None
        source = None
        acquired = False
        result = QueryOutcome("unknown")
        try:
            try:
                request = QueryRequest.parse(raw)
            except (ValueError, KeyError, TypeError, AttributeError):
                result = self._error("invalid_input", "INVALID_INPUT")
                return result
            source = self.sources.get(request.scope.source_id)
            if not source or not ScopePolicy().allows(
                principal, request.tool, request.scope, source.scopes
            ):
                result = self._error("denied", "SCOPE_DENIED")
                return result
            if source.kind != TOOL_KINDS[request.tool]:
                result = self._error("invalid_input", "SOURCE_KIND_MISMATCH")
                return result
            if (
                request.window
                and (request.window.end - request.window.start).total_seconds()
                > self.limits.window_seconds
            ):
                result = self._error("invalid_input", "WINDOW_LIMIT")
                return result
            if (
                self._active >= self.limits.global_concurrency
                or self._by_principal.get(principal.id, 0) >= self.limits.principal_concurrency
            ):
                result = self._error("unavailable", "OVERLOADED", True)
                return result
            self._active += 1
            self._by_principal[principal.id] = self._by_principal.get(principal.id, 0) + 1
            acquired = True
            gateway = self.gateways.get(source.id)
            if gateway is None:
                raise GatewayError("unavailable", "SOURCE_UNAVAILABLE")
            outgoing = request
            if request.parameters.get("cursor"):
                try:
                    if not self.cursor:
                        raise ValueError("Cursor not configured")
                    decoded = self.cursor.decode(
                        principal.id, request, request.parameters["cursor"]
                    )
                    outgoing = replace(
                        request, parameters={**request.parameters, "cursor": decoded}
                    )
                except ValueError:
                    result = self._error("invalid_input", "INVALID_CURSOR")
                    return result
            async with asyncio.timeout(self.deadline_seconds):
                data = await gateway.query(outgoing)
            if source.kind == "logs" and data.get("next_cursor"):
                if not self.cursor:
                    raise GatewayError("unknown", "CURSOR_NOT_CONFIGURED")
                data = {
                    **data,
                    "next_cursor": self.cursor.encode(principal.id, request, data["next_cursor"]),
                }
            records = data[RECORD_KEYS[source.kind]]
            status = "ok" if records else "empty"
            result = QueryOutcome(
                status,
                data,
                self._evidence(request, source, records, status),
                meta={
                    "invocation_id": request.context["invocation_id"],
                    "truncated": bool(data.get("truncated", False)),
                },
            )
            return result
        except TimeoutError:
            result = self._error("timeout", "DEADLINE_EXCEEDED", True)
            return result
        except GatewayError as exc:
            result = self._error(exc.status, exc.code, exc.retryable)
            return result
        except asyncio.CancelledError:
            result = self._error("unknown", "CANCELLED")
            raise
        except Exception:
            result = self._error("unknown", "INVALID_UPSTREAM_RESULT")
            return result
        finally:
            if acquired:
                self._active -= 1
                self._by_principal[principal.id] -= 1
            if request:
                result.meta.setdefault("invocation_id", request.context["invocation_id"])
                if source and acquired and not result.evidence_refs:
                    result.evidence_refs = self._evidence(request, source, [], "unavailable")
            event = {
                "observed_at": self.clock.now().isoformat(),
                "principal_id": principal.id,
                "status": result.status,
                "error_code": (result.error or {}).get("code"),
                "elapsed_ms": round((time.monotonic() - started) * 1000),
                "config_version": principal.policy.version,
            }
            if request:
                event.update(
                    request.context,
                    tool=request.tool,
                    source_id=request.scope.source_id,
                    scope=asdict(request.scope),
                    parameters_hash=hashlib.sha256(
                        json.dumps(request.parameters, sort_keys=True).encode()
                    ).hexdigest(),
                    truncated=result.meta.get("truncated", False),
                    record_count=len(result.data.get(RECORD_KEYS[TOOL_KINDS[request.tool]], [])),
                )
            self.audit.emit(event)

    def _evidence(self, request, source, records, status):
        quality = {"ok": "valid", "empty": "empty"}.get(status, "unavailable")
        selected = records if source.kind == "knowledge" and records else [{}]
        return [
            Evidence(
                str(uuid4()),
                source.kind,
                source.binding_id,
                request.context["invocation_id"],
                request.context["query_scope_ref"],
                request.scope,
                request.window,
                self.clock.now(),
                f"{source.kind} query: {status}",
                r.get("source_uri"),
                quality,
                r.get("source_id") or source.id,
            )
            for r in selected
        ]

    @staticmethod
    def _error(status, code, retryable=False):
        return QueryOutcome(
            status,
            error={"code": code, "message": "Query could not be completed"},
            retryable=retryable,
        )

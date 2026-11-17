from dataclasses import dataclass
from datetime import datetime
from typing import Any

from workbench_mcp_server.domain.scope import QueryScope, TimeWindow

TOOL_KINDS = {
    "knowledge_search": "knowledge",
    "metrics_query": "metrics",
    "logs_search": "logs",
    "trace_get": "trace",
}
RECORD_KEYS = {"knowledge": "matches", "metrics": "series", "logs": "entries", "trace": "spans"}


def bounded_text(value, minimum=1, maximum=128):
    if not isinstance(value, str) or not minimum <= len(value.strip()) <= maximum:
        raise ValueError("Invalid text parameter")
    return value


def bounded_integer(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError("Invalid integer parameter")
    return value


@dataclass(frozen=True)
class QueryRequest:
    tool: str
    scope: QueryScope
    context: dict[str, str]
    parameters: dict[str, Any]
    window: TimeWindow | None

    @classmethod
    def parse(cls, raw: dict):
        tool = raw["tool"]
        if tool not in TOOL_KINDS:
            raise ValueError("Unknown tool")
        scope = QueryScope(**raw["scope"])
        context = raw["context"]
        if set(context) != {"run_id", "invocation_id", "query_scope_ref"}:
            raise ValueError("Invalid correlation context")
        for value in context.values():
            bounded_text(value)
        params = {k: v for k, v in raw.items() if k not in {"tool", "scope", "context"}}
        window = None
        if tool == "knowledge_search":
            allowed = {"query", "scene", "limit"}
            bounded_text(params["query"], maximum=4000)
            if params["scene"] not in {"qa", "alert"}:
                raise ValueError("Invalid scene")
            params["limit"] = bounded_integer(params.get("limit", 3), 1, 10)
        else:
            window = TimeWindow(
                datetime.fromisoformat(params["start"]), datetime.fromisoformat(params["end"])
            )
            params.update(start=window.start.isoformat(), end=window.end.isoformat())
            allowed = {"start", "end"}
            if tool == "metrics_query":
                allowed |= {"template_id", "parameters", "step_seconds"}
                bounded_text(params["template_id"])
                if not isinstance(params.get("parameters", {}), dict):
                    raise ValueError("Invalid template parameters")
                params.setdefault("parameters", {})
                params["step_seconds"] = bounded_integer(params.get("step_seconds", 60), 15, 3600)
            elif tool == "logs_search":
                allowed |= {"levels", "keyword", "trace_id", "limit", "cursor"}
                levels = params.setdefault("levels", ["ERROR"])
                if (
                    not isinstance(levels, list)
                    or not levels
                    or any(x not in {"DEBUG", "INFO", "WARN", "ERROR", "FATAL"} for x in levels)
                ):
                    raise ValueError("Invalid log levels")
                params["limit"] = bounded_integer(params.get("limit", 50), 1, 200)
                for name, length in (("keyword", 256), ("trace_id", 128), ("cursor", 8192)):
                    if params.get(name) is not None:
                        bounded_text(params[name], maximum=length)
            else:
                allowed |= {"trace_id"}
                bounded_text(params["trace_id"])
        if set(params) - allowed:
            raise ValueError("Unknown query parameters")
        return cls(tool, scope, dict(context), params, window)


@dataclass(frozen=True)
class RuntimeSource:
    id: str
    kind: str
    binding_id: str
    scopes: frozenset[QueryScope]

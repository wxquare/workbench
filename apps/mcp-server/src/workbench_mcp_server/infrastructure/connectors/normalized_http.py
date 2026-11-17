from dataclasses import asdict

from pydantic import ValidationError

from workbench_mcp_server.application.results import GatewayError

from .http_base import HttpGateway
from .schemas import DATA_MODELS


class NormalizedHttpGateway(HttpGateway):
    async def query(self, request):
        if self.source.kind == "metrics":
            template = self.source.templates.get(request.parameters["template_id"])
            params = request.parameters["parameters"]
            if template is None or set(params) != set(template.parameters):
                raise GatewayError("invalid_input", "INVALID_METRIC_TEMPLATE")
            for key, rule in template.parameters.items():
                value = params[key]
                expected = str if rule.type == "string" else int
                if type(value) is not expected or value not in rule.allowed:
                    raise GatewayError("invalid_input", "INVALID_TEMPLATE_PARAMETER")
        payload = {
            "scope": asdict(request.scope),
            "parameters": request.parameters,
            "schema_version": "normalized-query-v1",
            "tool": request.tool,
        }
        raw = await self.post(payload)
        return self.validate(raw, request)

    def validate(self, raw, request):
        try:
            model = DATA_MODELS[self.source.kind].model_validate(raw)
        except (ValidationError, TypeError, ValueError):
            raise GatewayError("unknown", "UPSTREAM_SCHEMA_MISMATCH") from None
        result = model.model_dump(mode="json")
        kind = self.source.kind
        if kind == "knowledge":
            limit = request.parameters["limit"]
            if len(result["matches"]) > limit:
                result["truncated"] = True
                result["matches"] = result["matches"][:limit]
        elif kind == "metrics":
            if any(
                s["labels"].get("service", request.scope.service) != request.scope.service
                for s in result["series"]
            ):
                raise GatewayError("unknown", "UPSTREAM_SCOPE_MISMATCH")
            for series in model.series:
                if any(
                    not request.window.start <= t < request.window.end for t, v in series.points
                ):
                    raise GatewayError("unknown", "UPSTREAM_WINDOW_MISMATCH")
            if len(result["series"]) > 100:
                result["truncated"] = True
                result["series"] = result["series"][:100]
            remaining = 10000
            for series in result["series"]:
                if len(series["points"]) > remaining:
                    series["points"] = series["points"][:remaining]
                    result["truncated"] = True
                remaining -= len(series["points"])
        elif kind == "logs":
            if any(e.service != request.scope.service for e in model.entries):
                raise GatewayError("unknown", "UPSTREAM_SCOPE_MISMATCH")
            if any(
                not request.window.start <= e.timestamp < request.window.end for e in model.entries
            ):
                raise GatewayError("unknown", "UPSTREAM_WINDOW_MISMATCH")
            limit = request.parameters["limit"]
            if len(result["entries"]) > limit:
                result["entries"] = result["entries"][:limit]
                result["truncated"] = True
                result["next_cursor"] = None
            for entry in result["entries"]:
                if len(entry["message"]) > 2048:
                    entry["message"] = entry["message"][:2048]
                    result["truncated"] = True
        else:
            if model.trace_id != request.parameters["trace_id"]:
                raise GatewayError("unknown", "UPSTREAM_TRACE_MISMATCH")
            if any(
                not (request.window.start <= s.start <= s.end <= request.window.end)
                for s in model.spans
                if s.service == request.scope.service
            ):
                raise GatewayError("unknown", "UPSTREAM_WINDOW_MISMATCH")
            permitted = [s for s in result["spans"] if s["service"] == request.scope.service]
            omitted = len(result["spans"]) - len(permitted) + max(0, len(permitted) - 500)
            result["spans"] = permitted[:500]
            result["omitted_spans"] += omitted
            result["truncated"] |= omitted > 0
            ids = {s["span_id"] for s in result["spans"]}
            for span in result["spans"]:
                if span["parent_id"] not in ids:
                    span["parent_id"] = None
        return result

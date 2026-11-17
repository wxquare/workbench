from dataclasses import asdict

from workbench_mcp_server.application.results import GatewayError

from .normalized_http import NormalizedHttpGateway


class DpKnowledgeV2Gateway(NormalizedHttpGateway):
    """Anti-corruption layer for DP retrieve-v2, never trusting answer-only summaries."""

    async def query(self, request):
        payload = {
            "query": request.parameters["query"],
            "scene": request.parameters["scene"],
            "limit": request.parameters["limit"],
            "task_context": asdict(request.scope),
        }
        raw = await self.post(payload)
        if (
            raw.get("ok") is not True
            or not isinstance(raw.get("supporting_knowledge"), list)
            or not isinstance(raw.get("schema_version"), str)
            or not raw["schema_version"]
        ):
            raise GatewayError("unknown", "KNOWLEDGE_INVALID_RESULT")
        matches = []
        for row in raw.get("supporting_knowledge", []):
            if not isinstance(row, dict):
                raise GatewayError("unknown", "KNOWLEDGE_INVALID_RESULT")
            matches.append(
                {
                    "source_id": row.get("source_id"),
                    "title": row.get("title") or row.get("role") or "Knowledge",
                    "content": row.get("content"),
                    "source_uri": None,
                    "source_locator": row.get("source_path"),
                    "updated_at": None,
                }
            )
        converted = {
            "matches": matches,
            "guidance": raw.get("guidance", []),
            "hypotheses": raw.get("hypotheses", []),
            "release_id": raw.get("release_id"),
            "scope_filter": raw.get("scope_filter"),
        }
        return self.validate(converted, request)

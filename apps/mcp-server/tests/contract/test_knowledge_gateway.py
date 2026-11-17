import httpx
import pytest

from tests.support.fixtures import request_data, source_data
from tests.support.http_server import upstream


@pytest.mark.parametrize("adapter", ["normalized-http-v1", "dp-knowledge-v2"])
@pytest.mark.parametrize("has_match", [True, False])
async def test_knowledge_adapters_preserve_source_and_empty_semantics(adapter, has_match):
    from workbench_mcp_server.application.queries import QueryRequest
    from workbench_mcp_server.infrastructure.config.settings import SourceConfig
    from workbench_mcp_server.infrastructure.connectors.dp_knowledge_v2 import DpKnowledgeV2Gateway
    from workbench_mcp_server.infrastructure.connectors.normalized_http import NormalizedHttpGateway

    if adapter == "dp-knowledge-v2":
        body = {
            "ok": True,
            "schema_version": "knowledge-retrieve-v2",
            "supporting_knowledge": [
                {
                    "source_id": "known-source",
                    "content": "Known evidence",
                    "source_path": "guide.md",
                }
            ]
            if has_match
            else [],
        }
        cls = DpKnowledgeV2Gateway
    else:
        body = {
            "matches": [
                {
                    "source_id": "known-source",
                    "title": "Guide",
                    "content": "Known evidence",
                    "source_uri": None,
                    "updated_at": None,
                }
            ]
            if has_match
            else []
        }
        cls = NormalizedHttpGateway
    with upstream(body) as (endpoint, requests):
        config = source_data(endpoint=endpoint)
        config.update(adapter=adapter, isolation="exclusive-source")
        async with httpx.AsyncClient(trust_env=False) as client:
            data = await cls(SourceConfig.model_validate(config), client, "credential").query(
                QueryRequest.parse(request_data())
            )
    assert len(data["matches"]) == (1 if has_match else 0)
    if has_match:
        assert data["matches"][0]["source_id"] == "known-source"
        assert data["matches"][0]["content"] == "Known evidence"
    assert len(requests) == 1

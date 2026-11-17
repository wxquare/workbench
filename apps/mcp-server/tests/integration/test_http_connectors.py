import pytest

from tests.support.fixtures import known_match, request_data, source_data
from tests.support.http_server import upstream


async def invoke(endpoint, body=None, tool="knowledge_search", kind="knowledge", **extra):
    import httpx

    from workbench_mcp_server.application.queries import QueryRequest
    from workbench_mcp_server.infrastructure.config.settings import SourceConfig
    from workbench_mcp_server.infrastructure.connectors.normalized_http import NormalizedHttpGateway

    config = source_data(kind=kind, endpoint=endpoint)
    if kind == "metrics":
        config["templates"] = {"error_rate": {"parameters": {}}}
    config.update(extra)
    source = SourceConfig.model_validate(config)
    params = {"template_id": "error_rate"} if kind == "metrics" else {}
    if kind == "trace":
        params["trace_id"] = "trace-1"
    async with httpx.AsyncClient(trust_env=False) as client:
        gateway = NormalizedHttpGateway(source, client, "test-upstream-token", max_bytes=2048)
        return await gateway.query(QueryRequest.parse(request_data(tool, **params)))


async def test_normalized_query_uses_configured_endpoint_and_outbound_credential():
    with upstream({"matches": [known_match()]}) as (endpoint, requests):
        data = await invoke(endpoint)
    assert data["matches"][0]["source_id"] == "doc-7"
    path, headers, body = requests[0]
    assert path == "/query"
    assert headers["Authorization"] == "Bearer test-upstream-token"
    assert body["scope"]["environment"] == "test"
    assert body["parameters"]["query"] == "How to investigate?"
    assert "context" not in body


@pytest.mark.parametrize(
    "status,want,retry",
    [(401, "unavailable", False), (429, "unavailable", True), (503, "unavailable", True)],
)
async def test_upstream_error_is_not_an_empty_success_and_is_not_retried(status, want, retry):
    from workbench_mcp_server.application.results import GatewayError

    with upstream({"secret": "must-not-leak"}, status) as (endpoint, requests):
        with pytest.raises(GatewayError) as caught:
            await invoke(endpoint)
    assert caught.value.status == want
    assert caught.value.retryable == retry
    assert "must-not-leak" not in str(caught.value)
    assert len(requests) == 1


@pytest.mark.parametrize(
    "body", [b"not JSON", {"matches": "wrong"}, {"matches": [{}]}, b"x" * 3000]
)
async def test_malformed_and_oversized_results_are_rejected(body):
    from workbench_mcp_server.application.results import GatewayError

    with upstream(body) as (endpoint, _):
        with pytest.raises(GatewayError):
            await invoke(endpoint)


async def test_redirect_is_not_followed():
    from workbench_mcp_server.application.results import GatewayError

    with upstream({}, 302) as (endpoint, requests):
        with pytest.raises(GatewayError):
            await invoke(endpoint)
    assert len(requests) == 1


async def test_log_record_from_another_service_is_not_disclosed():
    from workbench_mcp_server.application.results import GatewayError

    data = {
        "entries": [
            {
                "timestamp": "2026-11-13T00:01:00Z",
                "service": "other",
                "level": "ERROR",
                "message": "private",
            }
        ]
    }
    with upstream(data) as (endpoint, _):
        with pytest.raises(GatewayError):
            await invoke(endpoint, tool="logs_search", kind="logs")


async def test_trace_removes_out_of_scope_span_and_cross_scope_parent():
    data = {
        "trace_id": "trace-1",
        "spans": [
            {
                "span_id": "a",
                "parent_id": None,
                "service": "other",
                "name": "private",
                "error": True,
                "start": "2026-11-13T00:01:00Z",
                "end": "2026-11-13T00:02:00Z",
            },
            {
                "span_id": "b",
                "parent_id": "a",
                "service": "checkout",
                "name": "purchase",
                "error": True,
                "start": "2026-11-13T00:01:00Z",
                "end": "2026-11-13T00:02:00Z",
            },
        ],
    }
    with upstream(data) as (endpoint, _):
        result = await invoke(endpoint, tool="trace_get", kind="trace")
    assert [s["span_id"] for s in result["spans"]] == ["b"]
    assert result["spans"][0]["parent_id"] is None
    assert result["omitted_spans"] == 1
    assert result["truncated"]


async def test_metric_template_rejects_unapproved_parameters_before_http():
    from workbench_mcp_server.application.results import GatewayError

    with upstream({"series": []}) as (endpoint, requests):
        with pytest.raises(GatewayError):
            await invoke(
                endpoint,
                tool="metrics_query",
                kind="metrics",
                templates={"other": {"parameters": {}}},
            )
    assert not requests


async def test_knowledge_v2_preserves_sources_without_fabricating_urls():
    import httpx

    from workbench_mcp_server.application.queries import QueryRequest
    from workbench_mcp_server.infrastructure.config.settings import SourceConfig
    from workbench_mcp_server.infrastructure.connectors.knowledge_v2 import KnowledgeV2Gateway

    payload = dict(
        ok=True,
        schema_version="2",
        supporting_knowledge=[
            dict(
                source_id="card-1",
                source_path="knowledge/guide.md",
                content="Check metrics",
                role="support",
            )
        ],
        guidance=[],
        hypotheses=[],
        release_id="release-1",
        scope_filter={"confirmed": {}, "rejected_candidates": 0},
    )
    with upstream(payload) as (endpoint, requests):
        config = source_data(endpoint=endpoint)
        config["adapter"] = "knowledge-v2"
        config["isolation"] = "exclusive-source"
        async with httpx.AsyncClient(trust_env=False) as client:
            data = await KnowledgeV2Gateway(
                SourceConfig.model_validate(config), client, "token"
            ).query(QueryRequest.parse(request_data()))
    assert data["matches"][0]["source_id"] == "card-1"
    assert data["matches"][0]["source_uri"] is None
    assert data["matches"][0]["source_locator"] == "knowledge/guide.md"
    assert data["release_id"] == "release-1"
    assert requests[0][2]["task_context"]["environment"] == "test"


@pytest.mark.parametrize(
    "body",
    [
        {"matches": [], "guidance": [{"content": "safe", "private_customer_record": "unapproved"}]},
        {
            "matches": [],
            "hypotheses": [{"content": "safe", "private_customer_record": "unapproved"}],
        },
        {"matches": [], "scope_filter": {"private_customer_record": "unapproved"}},
    ],
)
async def test_knowledge_arbitrary_nested_fields_are_not_returned(body):
    from workbench_mcp_server.application.results import GatewayError

    with upstream(body) as (endpoint, _):
        with pytest.raises(GatewayError):
            await invoke(endpoint)


@pytest.mark.parametrize(
    "trace_id,start,end",
    [
        ("other-trace", "2026-11-13T00:01:00Z", "2026-11-13T00:02:00Z"),
        ("trace-1", "2026-11-13T01:00:00Z", "2026-11-13T01:01:00Z"),
    ],
)
async def test_trace_evidence_rejects_wrong_trace_and_out_of_window_spans(trace_id, start, end):
    from workbench_mcp_server.application.results import GatewayError

    data = {
        "trace_id": trace_id,
        "spans": [
            {
                "span_id": "a",
                "parent_id": None,
                "service": "checkout",
                "name": "purchase",
                "error": True,
                "start": start,
                "end": end,
            }
        ],
    }
    with upstream(data) as (endpoint, _):
        with pytest.raises(GatewayError):
            await invoke(endpoint, tool="trace_get", kind="trace")


async def test_incomplete_knowledge_v2_success_is_not_treated_as_empty():
    import httpx

    from workbench_mcp_server.application.queries import QueryRequest
    from workbench_mcp_server.application.results import GatewayError
    from workbench_mcp_server.infrastructure.config.settings import SourceConfig
    from workbench_mcp_server.infrastructure.connectors.knowledge_v2 import KnowledgeV2Gateway

    with upstream({"ok": True}) as (endpoint, _):
        config = source_data(endpoint=endpoint)
        config.update(adapter="knowledge-v2", isolation="exclusive-source")
        async with httpx.AsyncClient(trust_env=False) as client:
            with pytest.raises(GatewayError):
                await KnowledgeV2Gateway(
                    SourceConfig.model_validate(config), client, "token"
                ).query(QueryRequest.parse(request_data()))

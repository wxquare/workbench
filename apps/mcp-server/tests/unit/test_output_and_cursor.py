import json
from dataclasses import replace

import pytest

from tests.support.fixtures import FixedClock, request_data


def test_cursor_is_bound_to_principal_scope_and_query_and_expires():
    from workbench_mcp_server.application.queries import QueryRequest
    from workbench_mcp_server.infrastructure.cursor import SignedCursor

    codec = SignedCursor("x" * 32, FixedClock())
    request = QueryRequest.parse(request_data("logs_search", keyword="checkout"))
    token = codec.encode("agent", request, "upstream-page-2")
    assert codec.decode("agent", request, token) == "upstream-page-2"
    with pytest.raises(ValueError):
        codec.decode("another-agent", request, token)
    with pytest.raises(ValueError):
        codec.decode(
            "agent", replace(request, parameters={**request.parameters, "keyword": "other"}), token
        )
    with pytest.raises(ValueError):
        codec.decode("agent", request, token[:-4] + "abcd")


def test_output_redacts_secrets_and_removes_unapproved_links():
    from workbench_mcp_server.application.results import QueryOutcome
    from workbench_mcp_server.interfaces.mcp.mapping import OutputMapper

    result = QueryOutcome(
        "ok",
        {
            "matches": [
                {
                    "source_id": "doc1",
                    "title": "t",
                    "content": "token=abc123 password=hunter2 Bearer example-credential",
                    "source_uri": "https://unknown.example/x?token=abc123",
                }
            ]
        },
    )
    mapped = OutputMapper(262144, ["example-credential"], []).map(result)
    rendered = json.dumps(mapped.structuredContent)
    assert "hunter2" not in rendered
    assert "abc123" not in rendered
    assert "example-credential" not in rendered
    assert "unknown.example" not in rendered


@pytest.mark.parametrize(
    "text,hidden",
    [
        ('{"password":"hunter2","token":"abc123"}', ["hunter2", "abc123"]),
        ("Authorization: Basic dXNlcjpwYXNz", ["dXNlcjpwYXNz"]),
        ("Cookie: session=private1; auth=private2", ["private1", "private2"]),
        ("event {'password': 'hunter2'}", ["hunter2"]),
    ],
)
def test_quoted_logs_and_full_authentication_headers_are_redacted(text, hidden):
    from workbench_mcp_server.application.results import QueryOutcome
    from workbench_mcp_server.interfaces.mcp.mapping import OutputMapper

    result = OutputMapper(262144, [], []).map(QueryOutcome("ok", {"entries": [{"message": text}]}))
    assert all(value not in result.model_dump_json() for value in hidden)


def test_output_limit_counts_both_json_copies_and_truncates_whole_records():
    from workbench_mcp_server.application.results import QueryOutcome
    from workbench_mcp_server.interfaces.mcp.mapping import OutputMapper

    result = QueryOutcome("ok", {"entries": [{"message": "中" * 2000} for _ in range(200)]})
    mapped = OutputMapper(8192, [], []).map(result)
    assert mapped.structuredContent["meta"]["truncated"]
    assert len(mapped.structuredContent["data"]["entries"]) < 200
    assert len(mapped.model_dump_json().encode()) <= 8192
    assert json.loads(mapped.content[0].text) == mapped.structuredContent


def test_local_log_truncation_clears_upstream_cursor_to_avoid_skipping_records():
    from workbench_mcp_server.application.results import QueryOutcome
    from workbench_mcp_server.interfaces.mcp.mapping import OutputMapper

    result = QueryOutcome(
        "ok",
        {"entries": [{"message": "中" * 2000} for _ in range(20)], "next_cursor": "after-all-20"},
    )
    payload = OutputMapper(8192, [], []).map(result).structuredContent
    assert payload["meta"]["truncated"]
    assert payload["data"]["next_cursor"] is None


def test_custom_redaction_fields_add_to_mandatory_credential_redaction():
    from workbench_mcp_server.application.results import QueryOutcome
    from workbench_mcp_server.interfaces.mcp.mapping import OutputMapper

    payload = {
        "password": "hunter2",
        "Customer_ID": "private-id",
        "entries": [
            {"message": '{"password":"hunter2","token":"abc123","customer_id":"private-id"}'}
        ],
    }
    result = OutputMapper(262144, [], [], ["Customer_ID"]).map(QueryOutcome("ok", payload))
    assert all(
        value not in result.model_dump_json() for value in ["hunter2", "abc123", "private-id"]
    )

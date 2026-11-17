import os
import sys
from pathlib import Path

import yaml
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from tests.support.fixtures import config_data, known_match, request_data, scope_data, source_data
from tests.support.http_server import upstream


async def test_all_four_tools_return_known_evidence_and_do_not_allow_cross_scope(tmp_path):
    samples = {
        "knowledge_search": {"matches": [known_match()]},
        "metrics_query": {
            "series": [
                {
                    "labels": {"service": "checkout"},
                    "points": [["2026-11-13T00:01:00Z", 0.12]],
                    "unit": "ratio",
                    "source_id": "metric-7",
                }
            ]
        },
        "logs_search": {
            "entries": [
                {
                    "timestamp": "2026-11-13T00:01:00Z",
                    "service": "checkout",
                    "level": "ERROR",
                    "message": "password=must-not-leak failure",
                    "record_id": "log-7",
                }
            ],
            "next_cursor": "raw-page-2",
        },
        "trace_get": {
            "trace_id": "trace-7",
            "spans": [
                {
                    "span_id": "span-7",
                    "parent_id": None,
                    "service": "checkout",
                    "name": "purchase",
                    "error": True,
                    "start": "2026-11-13T00:01:00Z",
                    "end": "2026-11-13T00:02:00Z",
                }
            ],
        },
    }

    def respond(payload):
        return samples[payload["tool"]]

    with upstream(respond) as (endpoint, requests):
        config = config_data(endpoint)
        config["sources"] = []
        config["principals"][0]["scopes"] = []
        config["cursor_secret_ref"] = "env://TEST_CURSOR_KEY"
        for name, kind in [
            ("kb", "knowledge"),
            ("metrics", "metrics"),
            ("logs", "logs"),
            ("trace", "trace"),
        ]:
            source = source_data(name, kind, endpoint)
            if kind == "metrics":
                source["templates"] = {"error_rate": {"parameters": {}}}
            config["sources"].append(source)
            config["principals"][0]["scopes"].append(scope_data(name))
        file = tmp_path / "config.yaml"
        file.write_text(yaml.safe_dump(config))
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "workbench_mcp_server.interfaces.cli", "serve", "--config", str(file)],
            env={
                **os.environ,
                "PYTHONPATH": str(Path("src").resolve()),
                "TEST_AGENT_TOKEN": "agent-token",
                "TEST_UPSTREAM_TOKEN": "upstream-token",
                "TEST_CURSOR_KEY": "x" * 32,
            },
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                for tool, source, extra, record_key in [
                    ("knowledge_search", "kb", {}, "matches"),
                    ("metrics_query", "metrics", {"template_id": "error_rate"}, "series"),
                    ("logs_search", "logs", {}, "entries"),
                    ("trace_get", "trace", {"trace_id": "trace-7"}, "spans"),
                ]:
                    args = request_data(tool, source, **extra)
                    args.pop("tool")
                    result = await client.call_tool(tool, args)
                    payload = result.structuredContent
                    assert payload["status"] == "ok", payload
                    assert payload["data"][record_key]
                    assert payload["evidence_refs"][0]["scope"]["source_id"] == source
                    assert "must-not-leak" not in result.model_dump_json()
                    if source == "logs":
                        assert payload["data"]["next_cursor"] != "raw-page-2"
                        args["cursor"] = payload["data"]["next_cursor"]
                        next_page = await client.call_tool(tool, args)
                        assert next_page.structuredContent["status"] == "ok"
                        args["keyword"] = "different-query"
                        rejected = await client.call_tool(tool, args)
                        assert rejected.structuredContent["status"] == "invalid_input"
                    args["scope"]["service"] = "unapproved"
                    denied = await client.call_tool(tool, args)
                    assert denied.structuredContent["status"] == "denied"
    assert len(requests) == 5
    assert requests[3][2]["parameters"]["cursor"] == "raw-page-2"

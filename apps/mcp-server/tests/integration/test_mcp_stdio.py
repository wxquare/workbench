import os
import sys
from pathlib import Path

import yaml
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from tests.support.fixtures import config_data, known_match, request_data
from tests.support.http_server import upstream


async def test_stdio_official_client_discovers_tools_and_queries_real_http(tmp_path):
    with upstream({"matches": [known_match()]}) as (endpoint, requests):
        config = tmp_path / "config.yaml"
        config.write_text(yaml.safe_dump(config_data(endpoint)))
        params = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "workbench_mcp_server.interfaces.cli",
                "serve",
                "--config",
                str(config),
                "--transport",
                "stdio",
            ],
            env={
                **os.environ,
                "PYTHONPATH": str(Path("src").resolve()),
                "TEST_AGENT_TOKEN": "test-agent-token",
                "TEST_UPSTREAM_TOKEN": "test-upstream-token",
            },
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                tools = (await client.list_tools()).tools
                assert {t.name for t in tools} == {
                    "knowledge_search",
                    "metrics_query",
                    "logs_search",
                    "trace_get",
                }
                assert all(t.outputSchema for t in tools)
                args = request_data()
                args.pop("tool")
                result = await client.call_tool("knowledge_search", args)
                assert not result.isError
                assert result.structuredContent["status"] == "ok"
                assert result.structuredContent["data"]["matches"][0]["source_id"] == "doc-7"
                args["scope"]["environment"] = "live"
                denied = await client.call_tool("knowledge_search", args)
                assert denied.isError
                assert denied.structuredContent["status"] == "denied"
    assert len(requests) == 1

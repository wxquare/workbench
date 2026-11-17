import os
import sys
from pathlib import Path

import pytest
import yaml
from mcp import ClientSession, McpError, StdioServerParameters
from mcp.client.stdio import stdio_client
from pydantic import AnyUrl

from tests.support.fixtures import config_data


async def test_stdio_exposes_diagnostic_guide_and_parameterized_prompt(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(config_data()))
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
            resources = (await client.list_resources()).resources
            uri = AnyUrl("dod://guides/incident-diagnosis")
            assert uri in [resource.uri for resource in resources]
            guide = await client.read_resource(uri)
            assert guide.contents[0].mimeType == "text/markdown"
            assert "knowledge_search" in guide.contents[0].text
            assert "source_id" in guide.contents[0].text

            prompts = (await client.list_prompts()).prompts
            prompt = next(p for p in prompts if p.name == "investigate_incident")
            assert {arg.name for arg in prompt.arguments if arg.required} == {
                "service",
                "environment",
                "start",
                "end",
            }
            arguments = {
                "service": "checkout",
                "environment": "test",
                "start": "2026-11-13T00:00:00Z",
                "end": "2026-11-13T00:10:00Z",
            }
            result = await client.get_prompt("investigate_incident", arguments)
            text = result.messages[0].content.text
            assert all(value in text for value in arguments.values())
            assert "source_id" in text
            assert "logs_search" in text
            with pytest.raises(McpError):
                await client.get_prompt("investigate_incident", {"service": "checkout"})

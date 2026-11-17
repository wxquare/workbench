"""Locate the MCP component in the DoD project for integration tests."""
from pathlib import Path


def mcp_server_source():
    path = Path(__file__).resolve().parents[3] / "mcp-server" / "src"
    assert path.is_dir(), f"MCP server source is missing: {path}"
    return path

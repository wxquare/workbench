import asyncio
import socket

import httpx
import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import AnyUrl

from tests.support.fixtures import config_data, known_match, request_data
from tests.support.http_server import upstream


async def test_http_authentication_and_official_client_query(monkeypatch):
    from workbench_mcp_server.bootstrap import Runtime
    from workbench_mcp_server.infrastructure.config.settings import Settings
    from workbench_mcp_server.interfaces.mcp.server import create_http_app

    monkeypatch.setenv("TEST_AGENT_TOKEN", "agent-http-token")
    monkeypatch.setenv("TEST_UPSTREAM_TOKEN", "upstream-http-token")
    with upstream({"matches": [known_match()]}) as (endpoint, requests):
        runtime = Runtime(Settings.model_validate(config_data(endpoint)))
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(create_http_app(runtime), log_level="error", lifespan="on")
        )
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            for _ in range(200):
                if server.started:
                    break
                await asyncio.sleep(0.01)
            assert server.started
            url = f"http://127.0.0.1:{port}/mcp"
            async with httpx.AsyncClient(trust_env=False) as http:
                denied = await http.post(url, headers={"Authorization": "Bearer wrong"}, json={})
                assert denied.status_code == 401
                for method, params in (
                    ("resources/read", {"uri": "dod://guides/incident-diagnosis"}),
                    ("prompts/get", {"name": "investigate_incident", "arguments": {}}),
                ):
                    denied = await http.post(
                        url,
                        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                    )
                    assert denied.status_code == 401
            async with httpx.AsyncClient(
                headers={"Authorization": "Bearer agent-http-token"}, trust_env=False
            ) as http:
                async with streamable_http_client(url, http_client=http) as (read, write, _):
                    async with ClientSession(read, write) as client:
                        await client.initialize()
                        guide = await client.read_resource(
                            AnyUrl("dod://guides/incident-diagnosis")
                        )
                        assert "knowledge_search" in guide.contents[0].text
                        prompt = await client.get_prompt(
                            "investigate_incident",
                            {
                                "service": "checkout",
                                "environment": "test",
                                "start": "2026-11-13T00:00:00Z",
                                "end": "2026-11-13T00:10:00Z",
                            },
                        )
                        assert "checkout" in prompt.messages[0].content.text
                        args = request_data()
                        args.pop("tool")
                        result = await client.call_tool("knowledge_search", args)
                        assert result.structuredContent["status"] == "ok"
            assert len(requests) == 1
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, 5)
            await runtime.close()


async def test_sdk_stateless_transport_cancels_gateway_on_disconnect(monkeypatch):
    import json

    from workbench_mcp_server.bootstrap import Runtime
    from workbench_mcp_server.infrastructure.config.settings import Settings
    from workbench_mcp_server.interfaces.mcp.server import create_http_app

    monkeypatch.setenv("TEST_AGENT_TOKEN", "agent-http-token")
    monkeypatch.setenv("TEST_UPSTREAM_TOKEN", "upstream-http-token")
    runtime = Runtime(Settings.model_validate(config_data()))
    running, cancelled = asyncio.Event(), asyncio.Event()

    class BlockingGateway:
        async def query(self, request):
            running.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    runtime.executor.gateways["kb"] = BlockingGateway()
    app = create_http_app(runtime)
    incoming = asyncio.Queue()
    args = request_data()
    args.pop("tool")
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "knowledge_search", "arguments": args},
        }
    ).encode()
    incoming.put_nowait({"type": "http.request", "body": body, "more_body": False})
    scope = dict(
        type="http",
        asgi={"version": "3.0"},
        http_version="1.1",
        method="POST",
        scheme="http",
        path="/mcp",
        raw_path=b"/mcp",
        query_string=b"",
        server=("127.0.0.1", 8000),
        client=("127.0.0.1", 12345),
        headers=[
            (b"host", b"127.0.0.1:8000"),
            (b"authorization", b"Bearer agent-http-token"),
            (b"content-type", b"application/json"),
            (b"accept", b"application/json, text/event-stream"),
            (b"mcp-protocol-version", b"2025-06-18"),
        ],
    )

    async def send(message):
        pass

    try:
        async with app.app.router.lifespan_context(app.app):
            task = asyncio.create_task(app(scope, incoming.get, send))
            try:
                await asyncio.wait_for(running.wait(), 2)
                incoming.put_nowait({"type": "http.disconnect"})
                await asyncio.wait_for(cancelled.wait(), 1)
                await asyncio.wait_for(task, 2)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    finally:
        await runtime.close()

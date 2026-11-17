import asyncio
from types import SimpleNamespace


async def test_http_disconnect_cancels_active_request():
    from workbench_mcp_server.interfaces.mcp.server import AuthenticatedApp

    running = asyncio.Event()
    stopped = asyncio.Event()
    incoming = asyncio.Queue()
    incoming.put_nowait({"type": "http.request", "body": b"{}", "more_body": False})

    async def app(scope, receive, send):
        await receive()
        running.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    class Auth:
        def authenticate(self, token):
            return SimpleNamespace(id="agent")

    async def send(message):
        pass

    task = asyncio.create_task(
        AuthenticatedApp(app, Auth())(
            {"type": "http", "headers": [(b"authorization", b"Bearer test")]}, incoming.get, send
        )
    )
    try:
        await asyncio.wait_for(running.wait(), 1)
        incoming.put_nowait({"type": "http.disconnect"})
        await asyncio.wait_for(stopped.wait(), 0.1)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

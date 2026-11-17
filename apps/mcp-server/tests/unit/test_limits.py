import asyncio
from datetime import timedelta

import pytest

from tests.support.fixtures import FixedClock, Gateway, RecordingAudit, config_data, request_data


async def test_principal_overload_is_immediate_and_recovers_after_cancellation():
    from workbench_mcp_server.application.use_cases import QueryExecutor
    from workbench_mcp_server.infrastructure.config.settings import Settings

    entered = asyncio.Queue()
    release = asyncio.Event()

    class BlockingGateway(Gateway):
        async def query(self, request):
            await entered.put(True)
            await release.wait()
            return {"matches": []}

    settings = Settings.model_validate(config_data())
    executor = QueryExecutor(
        settings.runtime_sources(),
        {"kb": BlockingGateway()},
        settings.limits,
        FixedClock(),
        RecordingAudit(),
    )
    principal = settings.principal("agent")
    tasks = [asyncio.create_task(executor.execute(principal, request_data())) for _ in range(2)]
    try:
        await asyncio.wait_for(entered.get(), 1)
        await asyncio.wait_for(entered.get(), 1)
        result = await asyncio.wait_for(executor.execute(principal, request_data()), 0.1)
        assert result.error["code"] == "OVERLOADED"
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    release.set()
    assert (await executor.execute(principal, request_data())).status == "empty"


def test_expired_cursor_is_rejected():
    from workbench_mcp_server.application.queries import QueryRequest
    from workbench_mcp_server.infrastructure.cursor import SignedCursor

    class LaterClock:
        def now(self):
            return FixedClock().now() + timedelta(seconds=301)

    request = QueryRequest.parse(request_data("logs_search"))
    token = SignedCursor("x" * 32, FixedClock()).encode("agent", request, "page2")
    with pytest.raises(ValueError):
        SignedCursor("x" * 32, LaterClock()).decode("agent", request, token)

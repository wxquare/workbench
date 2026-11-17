import asyncio

import pytest

from tests.support.fixtures import FixedClock, Gateway, RecordingAudit, config_data, request_data


def build(gateway=None, data=None):
    from workbench_mcp_server.application.use_cases import QueryExecutor
    from workbench_mcp_server.infrastructure.config.settings import Settings

    settings = Settings.model_validate(config_data())
    audit = RecordingAudit()
    gateway = gateway or Gateway(data)
    executor = QueryExecutor(
        settings.runtime_sources(), {"kb": gateway}, settings.limits, FixedClock(), audit
    )
    return executor, settings.principal("agent"), gateway, audit


async def test_denied_query_never_reaches_gateway():
    executor, principal, gateway, audit = build()
    args = request_data()
    args["scope"]["environment"] = "live"
    result = await executor.execute(principal, args)
    assert result.status == "denied"
    assert gateway.calls == []
    assert audit.events[-1]["status"] == "denied"


async def test_success_carries_real_source_scope_and_fixed_observation():
    executor, principal, gateway, audit = build()
    result = await executor.execute(principal, request_data())
    assert result.status == "ok"
    assert result.data["matches"][0]["source_id"] == "doc-7"
    evidence = result.evidence_refs[0]
    assert evidence.source_locator == "doc-7"
    assert evidence.scope.service == "checkout"
    assert evidence.query_scope_ref == "scope1"
    assert evidence.observed_at.isoformat() == "2026-11-13T00:00:00+00:00"
    assert "How to investigate?" not in str(audit.events)


async def test_empty_is_a_successful_query_without_matches():
    executor, principal, gateway, audit = build(data={"matches": []})
    result = await executor.execute(principal, request_data())
    assert result.status == "empty"
    assert result.error is None
    assert result.evidence_refs[0].quality_status == "empty"
    assert len(gateway.calls) == 1


async def test_unavailable_is_not_empty_and_does_not_expose_exception():
    from workbench_mcp_server.application.results import GatewayError

    executor, principal, gateway, audit = build(
        Gateway(error=GatewayError("unavailable", "UPSTREAM_UNAVAILABLE", True))
    )
    result = await executor.execute(principal, request_data())
    assert result.status == "unavailable"
    assert result.retryable
    assert result.evidence_refs[0].quality_status == "unavailable"


async def test_invalid_input_is_rejected_before_gateway():
    executor, principal, gateway, audit = build()
    result = await executor.execute(principal, request_data(query=""))
    assert result.status == "invalid_input"
    assert not gateway.calls


async def test_timeout_is_distinct_and_attempt_is_not_retried():
    class SlowGateway(Gateway):
        async def query(self, request):
            self.calls.append(request)
            await asyncio.sleep(10)

    executor, principal, gateway, audit = build(SlowGateway())
    executor.deadline_seconds = 0.01
    result = await executor.execute(principal, request_data())
    assert result.status == "timeout"
    assert len(gateway.calls) == 1


async def test_cancellation_propagates_and_is_audited():
    class CancelledGateway(Gateway):
        async def query(self, request):
            raise asyncio.CancelledError

    executor, principal, gateway, audit = build(CancelledGateway())
    with pytest.raises(asyncio.CancelledError):
        await executor.execute(principal, request_data())
    assert audit.events[-1]["error_code"] == "CANCELLED"

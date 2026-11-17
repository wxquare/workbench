import json
import logging

import pytest
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from workbench_agent.observability import JsonFormatter, Telemetry, bind_context, redacted_content
from workbench_agent.settings import ObservabilitySettings


def test_model_logger_uses_configured_workbench_root():
    root = logging.getLogger('workbench')
    from workbench_agent.runtime.models.invoker import logger

    assert logger.parent is root


def test_json_logging_keeps_context_and_drops_untrusted_extra():
    record = logging.LogRecord('workbench.models', logging.INFO, '', 0, 'model attempt', (), None)
    record.event = 'model.attempt'
    record.authorization = 'Bearer super-secret'
    formatter = JsonFormatter('worker', 'test')
    with bind_context(run_id='run-1', team_id='team-1'):
        line = formatter.format(record)
    payload = json.loads(line)
    assert payload['run_id'] == 'run-1'
    assert payload['event'] == 'model.attempt'
    assert 'super-secret' not in line


def test_context_does_not_leak_after_scope():
    record = logging.LogRecord('workbench', logging.INFO, '', 0, 'safe', (), None)
    formatter = JsonFormatter('worker', 'test')
    with bind_context(run_id='run-1'):
        assert json.loads(formatter.format(record))['run_id'] == 'run-1'
    assert 'run_id' not in json.loads(formatter.format(record))


def test_logging_redacts_sensitive_values_even_in_allowed_fields(monkeypatch):
    monkeypatch.setenv('WORKBENCH_TEST_SECRET', 'real-private-secret')
    record = logging.LogRecord('workbench', logging.INFO, '', 0, 'safe', (), None)
    record.model_id = 'Bearer real-private-secret'
    formatted = JsonFormatter('worker', 'test').format(record)
    assert 'real-private-secret' not in formatted
    assert '[REDACTED]' in formatted


def test_cross_process_traceparent_connects_distinct_providers():
    exporter = InMemorySpanExporter()
    settings = ObservabilitySettings.model_validate({'tracing': {'enabled': True}})
    api = Telemetry(settings, 'api', 'test', processor=SimpleSpanProcessor(exporter))
    worker = Telemetry(settings, 'worker', 'test', processor=SimpleSpanProcessor(exporter))
    with api.span('run.accept'):
        parent = api.traceparent()
    with worker.span('job.execute', traceparent=parent):
        pass
    spans = exporter.get_finished_spans()
    assert spans[0].instrumentation_scope.name == 'workbench.observability'
    assert spans[1].context.trace_id == spans[0].context.trace_id
    assert spans[1].parent.span_id == spans[0].context.span_id
    api.shutdown()
    worker.shutdown()


def test_unsampled_parent_stays_unsampled_across_providers():
    exporter = InMemorySpanExporter()
    api = Telemetry(ObservabilitySettings.model_validate({'tracing': {'enabled': True, 'sample_ratio': 0}}),
                    'api', 'test', processor=SimpleSpanProcessor(exporter))
    worker = Telemetry(ObservabilitySettings.model_validate({'tracing': {'enabled': True, 'sample_ratio': 1}}),
                       'worker', 'test', processor=SimpleSpanProcessor(exporter))
    try:
        with api.span('run.accept'):
            parent = api.traceparent()
        with worker.span('job.execute', traceparent=parent):
            pass
        assert exporter.get_finished_spans() == ()
    finally:
        api.shutdown()
        worker.shutdown()


def test_langfuse_requires_explicit_credentials_and_tracing():
    with pytest.raises(ValueError):
        ObservabilitySettings.model_validate({'langfuse': {'enabled': True}})


def test_enabled_langfuse_requires_explicit_deployment_credentials(monkeypatch):
    monkeypatch.delenv('LANGFUSE_BASE_URL', raising=False)
    config = ObservabilitySettings.model_validate({'tracing': {'enabled': True}, 'langfuse': {'enabled': True}})
    with pytest.raises(ValueError):
        Telemetry(config, 'worker', 'test')


def test_content_redaction_removes_environment_secrets_and_respects_utf8_limit(monkeypatch):
    monkeypatch.setenv('WORKBENCH_TEST_SECRET', 'private-key-123')
    captured, truncated = redacted_content('你好 private-key-123 example@example.com', 32)
    assert 'private-key-123' not in captured
    assert 'example@example.com' not in captured
    assert len(captured.encode('utf-8')) <= 32
    assert truncated


def test_content_redaction_handles_json_escaped_secrets(monkeypatch):
    secret = 'key-"with\\escaped'
    monkeypatch.setenv('WORKBENCH_TEST_SECRET', secret)
    captured, truncated = redacted_content({'content': f'private {secret}'}, 1024)
    assert not truncated
    assert secret not in json.loads(captured)['content']
    assert '[REDACTED]' in captured


def test_langfuse_sdk_exports_our_spans_without_credentials_in_attributes(monkeypatch):
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.trace.export import SpanExportResult
    from uuid import uuid4

    exported = []
    monkeypatch.setattr(OTLPSpanExporter, 'export', lambda self, spans: (exported.extend(spans), SpanExportResult.SUCCESS)[1])
    monkeypatch.setenv('LANGFUSE_BASE_URL', 'https://langfuse.example.test')
    monkeypatch.setenv('LANGFUSE_PUBLIC_KEY', f'pk-test-{uuid4()}')
    monkeypatch.setenv('LANGFUSE_SECRET_KEY', 'sk-test-private-value')
    settings = ObservabilitySettings.model_validate({'tracing': {'enabled': True}, 'langfuse': {'enabled': True}})
    telemetry = Telemetry(settings, 'worker', 'test')
    try:
        with telemetry.span('model.generate', attributes={'langfuse.observation.type': 'generation',
                'session.id': 'thread-test', 'workbench.invocation_id': 'invocation-test'}):
            pass
        assert telemetry.provider.force_flush(timeout_millis=5000)
        assert len(exported) == 1
        assert exported[0].name == 'model.generate'
        assert exported[0].attributes['session.id'] == 'thread-test'
        assert 'sk-test-private-value' not in str(exported[0].attributes)
    finally:
        telemetry.shutdown()


def test_langfuse_export_failure_does_not_change_span_body(monkeypatch):
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.trace.export import SpanExportResult
    from uuid import uuid4

    attempts = []
    monkeypatch.setattr(OTLPSpanExporter, 'export', lambda self, spans: (attempts.append(len(spans)), SpanExportResult.FAILURE)[1])
    monkeypatch.setenv('LANGFUSE_BASE_URL', 'https://langfuse.example.test')
    monkeypatch.setenv('LANGFUSE_PUBLIC_KEY', f'pk-test-{uuid4()}')
    monkeypatch.setenv('LANGFUSE_SECRET_KEY', 'sk-test-private-value')
    settings = ObservabilitySettings.model_validate({'tracing': {'enabled': True}, 'langfuse': {'enabled': True}})
    telemetry = Telemetry(settings, 'worker', 'test')
    try:
        with telemetry.span('job.execute'):
            result = 'business-result'
        telemetry.provider.force_flush(timeout_millis=5000)
        assert result == 'business-result'
        assert attempts
    finally:
        telemetry.shutdown()


@pytest.mark.asyncio
async def test_shutdown_timeout_does_not_wait_for_stuck_exporter():
    import threading
    import time
    from workbench_agent.observability import shutdown_telemetry

    release = threading.Event()
    class SlowTelemetry:
        def shutdown(self):
            release.wait(2)

    started = time.monotonic()
    try:
        await shutdown_telemetry(SlowTelemetry(), timeout=0.03)
        assert time.monotonic() - started < 0.5
    finally:
        release.set()


@pytest.mark.asyncio
async def test_scheduler_tick_is_a_short_independent_trace(monkeypatch):
    from types import SimpleNamespace
    from workbench_agent.jobs.scheduler import Scheduler, TickReport

    exporter = InMemorySpanExporter()
    telemetry = Telemetry(ObservabilitySettings.model_validate({'tracing': {'enabled': True}}),
                          'scheduler', 'test', processor=SimpleSpanProcessor(exporter))
    scheduler = object.__new__(Scheduler)
    scheduler.backend = SimpleNamespace(_telemetry=telemetry)
    async def tick():
        return TickReport(scanned=1)
    monkeypatch.setattr(scheduler, '_tick', tick, raising=False)
    try:
        assert (await scheduler.tick()).scanned == 1
        assert [span.name for span in exporter.get_finished_spans()] == ['scheduler.tick']
    finally:
        telemetry.shutdown()

"""Bounded, opt-in telemetry; business state never depends on the exporter."""
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
import json
import logging
import os
import re
import sys
from urllib.parse import urlsplit
from uuid import uuid4

from opentelemetry.context import Context
from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

_context = ContextVar('dod_log_context', default={})
_LOG_FIELDS = frozenset({'request_id', 'team_id', 'agent_id', 'thread_id', 'run_id', 'job_id',
    'run_epoch', 'invocation_id', 'attempt', 'status', 'duration_ms', 'queue_wait_ms',
    'model_id', 'role', 'input_tokens', 'output_tokens', 'graph_id', 'error_code'})
_EVENT = re.compile(r'^[a-z][a-z0-9_.-]{0,79}$')


def _redact_text(content, *, json_encoded=False):
    for key, secret in os.environ.items():
        if re.search(r'(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)', key, re.I) and len(secret) >= 4:
            content = content.replace(secret, '[REDACTED]')
            if json_encoded:
                content = content.replace(json.dumps(secret, ensure_ascii=False)[1:-1], '[REDACTED]')
    content = re.sub(r'(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*', 'Bearer [REDACTED]', content)
    return re.sub(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', '[REDACTED_EMAIL]', content)


@contextmanager
def bind_context(**fields):
    accepted = {key: value for key, value in fields.items() if key in _LOG_FIELDS and value is not None}
    token = _context.set({**_context.get(), **accepted})
    try:
        yield
    finally:
        _context.reset(token)


class JsonFormatter(logging.Formatter):
    def __init__(self, service, environment):
        super().__init__()
        self.service, self.environment = service, environment

    def format(self, record):
        event = getattr(record, 'event', 'log.record')
        if not isinstance(event, str) or not _EVENT.fullmatch(event):
            event = 'log.record'
        payload = {'timestamp': datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            'level': record.levelname, 'service': self.service, 'environment': self.environment,
            'event': event, 'message': event}
        for key, value in {**_context.get(), **record.__dict__}.items():
            if key in _LOG_FIELDS and isinstance(value, (int, float, str)) and not isinstance(value, bool):
                payload[key] = _redact_text(value)[:512] if isinstance(value, str) else value
        span_context = trace.get_current_span().get_span_context()
        if span_context.is_valid:
            payload['trace_id'] = f'{span_context.trace_id:032x}'
            payload['span_id'] = f'{span_context.span_id:016x}'
        encoded = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
        if len(encoded.encode('utf-8')) > 32768:
            payload = {key: payload[key] for key in ('timestamp', 'level', 'service', 'environment', 'event')}
            payload['truncated'] = True
            encoded = json.dumps(payload, separators=(',', ':'))
        return encoded


def configure_logging(settings, service, environment):
    logger = logging.getLogger('workbench')
    logger.setLevel(settings.logging.level)
    logger.propagate = False
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter(service, environment))
    logger.handlers[:] = [handler]


class Telemetry:
    def __init__(self, settings, service, environment, *, processor=None):
        self.settings = settings
        self.provider = None
        self.client = None
        self.tracer = None
        if not settings.tracing.enabled:
            return
        self.provider = TracerProvider(
            resource=Resource.create({'service.name': f'dod-{service}', 'deployment.environment.name': environment}),
            sampler=ParentBased(TraceIdRatioBased(settings.tracing.sample_ratio)),
            shutdown_on_exit=False)
        if processor is not None:
            self.provider.add_span_processor(processor)
        if settings.langfuse.enabled:
            from workbench_agent.settings import resolve_env_reference
            from langfuse import Langfuse
            base_url = resolve_env_reference(settings.langfuse.base_url_ref)
            parsed = urlsplit(base_url)
            if (parsed.scheme != 'https' and not (environment != 'production' and parsed.scheme == 'http'
                    and parsed.hostname in ('localhost', '127.0.0.1', '::1'))) or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError('invalid Langfuse endpoint')
            self.client = Langfuse(base_url=base_url, public_key=resolve_env_reference(settings.langfuse.public_key_ref),
                secret_key=resolve_env_reference(settings.langfuse.secret_key_ref), environment=environment,
                tracer_provider=self.provider, timeout=5,
                should_export_span=lambda span: bool(span.instrumentation_scope and span.instrumentation_scope.name == 'workbench.observability'))
        self.tracer = self.provider.get_tracer('workbench.observability')

    @contextmanager
    def span(self, name, *, traceparent=None, attributes=None, root=False):
        if self.tracer is None:
            yield None
            return
        parent = Context() if root else None
        if traceparent:
            parent = TraceContextTextMapPropagator().extract({'traceparent': traceparent})
        with self.tracer.start_as_current_span(name, context=parent, record_exception=False,
                set_status_on_exception=False, attributes=attributes or {}) as span:
            yield span

    @staticmethod
    def traceparent():
        carrier = {}
        TraceContextTextMapPropagator().inject(carrier)
        return carrier.get('traceparent')

    def shutdown(self):
        if self.provider is not None:
            self.provider.force_flush(timeout_millis=5000)
            self.provider.shutdown()
        if self.client is not None:
            self.client.shutdown()


def current_telemetry(backend):
    return getattr(backend, '_telemetry', None)


async def shutdown_telemetry(telemetry, *, timeout=5):
    import asyncio
    import threading

    done = threading.Event()

    def close():
        try:
            telemetry.shutdown()
        except Exception:
            logging.getLogger('workbench.telemetry').warning('telemetry shutdown failed',
                extra={'event': 'telemetry.shutdown_failed'})
        finally:
            done.set()

    threading.Thread(target=close, name='dod-telemetry-shutdown', daemon=True).start()
    deadline = asyncio.get_running_loop().time() + timeout
    while not done.is_set():
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            logging.getLogger('workbench.telemetry').warning('telemetry shutdown timed out',
                extra={'event': 'telemetry.shutdown_timeout'})
            return
        await asyncio.sleep(min(0.05, remaining))


def redacted_content(value, limit):
    """Redact known credentials and common identifiers before truncating UTF-8."""
    try:
        content = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
        content = _redact_text(content, json_encoded=True)
        raw = content.encode('utf-8')
        return raw[:limit].decode('utf-8', errors='ignore'), len(raw) > limit
    except Exception:
        return None, False


def span_or_none(backend, name, **kwargs):
    telemetry = current_telemetry(backend)
    return telemetry.span(name, **kwargs) if telemetry is not None else nullcontext(None)


def observe_run_admission(function):
    @wraps(function)
    async def wrapped(self, auth, request, *args, **kwargs):
        with bind_context(team_id=auth.team_id, agent_id=str(request.agent_id)):
            with span_or_none(self.backend, 'run.accept') as span:
                result = await function(self, auth, request, *args, **kwargs)
                if span is not None:
                    span.set_attribute('workbench.run_id', str(result.run_id))
                    span.set_attribute('session.id', str(result.thread_id))
                logging.getLogger('workbench.runs').info('run.accepted', extra={'event': 'run.accepted',
                    'run_id': str(result.run_id), 'thread_id': str(result.thread_id)})
                return result
    return wrapped


class RequestObservabilityMiddleware:
    def __init__(self, app, backend):
        self.app, self.backend = app, backend

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        request_id = str(uuid4())
        status = 500

        async def capture(message):
            nonlocal status
            if message['type'] == 'http.response.start':
                status = message['status']
            await send(message)

        import time
        started = time.monotonic()
        with bind_context(request_id=request_id):
            with span_or_none(self.backend, 'api.request', root=True) as span:
                try:
                    return await self.app(scope, receive, capture)
                finally:
                    if span is not None:
                        span.set_attribute('http.response.status_code', status)
                    logging.getLogger('workbench.api').info('request.completed', extra={'event': 'request.completed',
                        'status': str(status), 'duration_ms': int((time.monotonic() - started) * 1000)})

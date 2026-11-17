from datetime import datetime, timezone


def scope_data(source="kb"):
    return dict(team="DP", environment="test", region="id", service="checkout", source_id=source)


def request_data(tool="knowledge_search", source="kb", **params):
    value = dict(
        tool=tool,
        scope=scope_data(source),
        context=dict(run_id="run1", invocation_id="call1", query_scope_ref="scope1"),
    )
    if tool == "knowledge_search":
        value.update(query="How to investigate?", scene="qa", limit=3)
    else:
        value.update(start="2026-11-13T00:00:00Z", end="2026-11-13T00:10:00Z")
    value.update(params)
    return value


def source_data(source="kb", kind="knowledge", endpoint="http://127.0.0.1:8765/query"):
    return dict(
        id=source,
        kind=kind,
        adapter="normalized-http-v1",
        endpoint=endpoint,
        auth_secret_ref="env://TEST_UPSTREAM_TOKEN",
        binding_id=f"binding-{source}",
        scopes=[scope_data(source)],
        scope_enforced=True,
    )


def config_data(endpoint="http://127.0.0.1:8765/query"):
    tools = ["knowledge_search", "metrics_query", "logs_search", "trace_get"]
    return dict(
        server=dict(local_development=True, stdio_principal="agent"),
        principals=[
            dict(
                id="agent",
                token_secret_ref="env://TEST_AGENT_TOKEN",
                tools=tools,
                scopes=[scope_data()],
            )
        ],
        sources=[source_data(endpoint=endpoint)],
    )


def known_match():
    return dict(
        source_id="doc-7",
        title="Checkout guide",
        content="Check the error rate.",
        source_uri="https://kb.example.test/docs/7",
        updated_at=None,
    )


class FixedClock:
    def now(self):
        return datetime(2026, 11, 13, tzinfo=timezone.utc)


class RecordingAudit:
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)


class Gateway:
    def __init__(self, data=None, error=None):
        self.data = data if data is not None else {"matches": [known_match()]}
        self.error = error
        self.calls = []

    async def query(self, request):
        self.calls.append(request)
        if self.error:
            raise self.error
        return self.data

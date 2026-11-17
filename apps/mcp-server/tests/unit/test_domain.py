from datetime import datetime, timezone

import pytest


def test_scope_requires_all_dimensions_and_rejects_wildcards():
    from workbench_mcp_server.domain.scope import QueryScope

    for value in ("", "*", "all"):
        with pytest.raises(ValueError):
            QueryScope("ExampleTeam", "test", "id", value, "logs")


def test_authorization_checks_complete_tuple_not_cartesian_product():
    from workbench_mcp_server.domain.identity import Principal
    from workbench_mcp_server.domain.policy import AccessPolicy, ScopePolicy
    from workbench_mcp_server.domain.scope import QueryScope

    a = QueryScope("ExampleTeam", "test", "id", "checkout", "logs")
    b = QueryScope("ExampleTeam", "staging", "sg", "catalog", "logs")
    policy = AccessPolicy("v1", frozenset({"logs_search"}), frozenset({a, b}))
    principal = Principal("worker", policy)
    assert ScopePolicy().allows(principal, "logs_search", a, frozenset({a}))
    mixed = QueryScope("ExampleTeam", "test", "sg", "catalog", "logs")
    assert not ScopePolicy().allows(principal, "logs_search", mixed, frozenset({mixed}))
    assert not ScopePolicy().allows(principal, "trace_get", a, frozenset({a}))
    assert not ScopePolicy().allows(principal, "logs_search", a, frozenset({b}))


@pytest.mark.parametrize(
    "start,end",
    [
        ("2026-11-13T00:00:00", "2026-11-13T00:10:00Z"),
        ("2026-11-13T00:10:00Z", "2026-11-13T00:00:00Z"),
        ("2026-11-13T00:00:00Z", "2026-11-13T01:00:01Z"),
    ],
)
def test_time_window_rejects_naive_reversed_and_excessive_ranges(start, end):
    from workbench_mcp_server.domain.scope import TimeWindow

    with pytest.raises(ValueError):
        TimeWindow(datetime.fromisoformat(start), datetime.fromisoformat(end))


def test_time_window_normalizes_to_utc():
    from workbench_mcp_server.domain.scope import TimeWindow

    window = TimeWindow(
        datetime.fromisoformat("2026-11-13T08:00:00+08:00"),
        datetime.fromisoformat("2026-11-13T08:10:00+08:00"),
    )
    assert window.start == datetime(2026, 11, 13, tzinfo=timezone.utc)


def test_valid_evidence_requires_a_source_and_observation():
    from workbench_mcp_server.domain.evidence import Evidence
    from workbench_mcp_server.domain.scope import QueryScope

    scope = QueryScope("ExampleTeam", "test", "id", "checkout", "kb")
    with pytest.raises(ValueError):
        Evidence(
            "e1",
            "knowledge",
            "binding",
            "i1",
            "s1",
            scope,
            None,
            datetime.now(timezone.utc),
            "summary",
            None,
            "valid",
            "",
        )

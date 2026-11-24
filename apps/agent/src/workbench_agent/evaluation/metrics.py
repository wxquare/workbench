"""Repeat-aware metrics with missing observations retained."""

from __future__ import annotations

import random
import statistics


def apply_dataset_boundary(report: dict, *, dataset_kind: str) -> dict:
    if dataset_kind not in {'business', 'synthetic'}:
        raise ValueError('DATASET_KIND_REQUIRED')
    value = {**report, 'dataset_kind': dataset_kind}
    if dataset_kind == 'synthetic':
        value.update(quality_status='unavailable', release_passed=False)
    return value


def summarize_repeats(scores: list[dict], *, expected_cases: list[str], k: int) -> dict:
    if type(k) is not int or not 1 <= k <= 5:
        raise ValueError('k must be between 1 and 5')
    by_case: dict[str, list[dict]] = {case: [] for case in expected_cases}
    for score in scores:
        case_id = score.get('case_id')
        if case_id in by_case:
            by_case[case_id].append(score)
    missing = sum(max(0, k - len(by_case[case])) for case in expected_cases)
    pass_at: list[int] = []
    pass_power: list[int] = []
    for case in expected_cases:
        values = by_case[case]
        indexes = {value.get('repeat') for value in values}
        complete = len(values) == k and indexes == set(range(k))
        statuses = [value.get('rules_status') for value in values]
        pass_at.append(int(any(status == 'passed' for status in statuses)))
        pass_power.append(int(complete and all(status == 'passed' for status in statuses)))
    rates = [
        sum(value.get('rules_status') == 'passed' for value in by_case[case]) / k
        for case in expected_cases
    ]
    observed = [value for case in expected_cases for value in by_case[case]]
    latency = [
        value.get('latency_ms')
        for value in observed
        if type(value.get('latency_ms')) in (int, float)
    ]
    costs: dict[str, int] = {}
    pending = 0
    for value in observed:
        usage = value.get('usage') or {}
        amount, currency = usage.get('amount_micros'), usage.get('currency')
        if type(amount) is int and isinstance(currency, str):
            costs[currency] = costs.get(currency, 0) + amount
        if usage.get('cost_pending') or usage.get('usage_pending') or amount is None:
            pending += 1
    rng = random.Random(20261120)
    intervals = []
    for _ in range(1000):
        sample = [rng.choice(rates) for _ in rates] if rates else []
        intervals.append(sum(sample) / len(sample) if sample else 0)
    intervals.sort()
    runnable = [
        value
        for value in observed
        if value.get('rules_status') in {'passed', 'failed', 'needs_review'}
    ]
    rule_rate = (
        sum(value.get('rules_status') == 'passed' for value in runnable) / len(runnable)
        if runnable
        else None
    )
    return {
        'schema_version': 'eval-metrics-v1',
        'cases': len(expected_cases),
        'k': k,
        'missing_runs': missing,
        'complete': missing == 0,
        'empirical_pass_at_k': sum(pass_at) / len(pass_at) if pass_at else None,
        'empirical_pass_power_k': sum(pass_power) / len(pass_power) if pass_power else None,
        'repeat_rates': rates,
        'rule_rate': rule_rate,
        'rule_n': len(runnable),
        'unauthorized_accesses': sum(
            'unauthorized_access' in (value.get('reasons') or []) for value in observed
        ),
        'incorrect_conclusions': sum(
            'unanswerable_conclusion' in (value.get('reasons') or []) for value in observed
        ),
        'variance': statistics.pvariance(rates) if len(rates) > 1 else 0,
        'latency_p50': statistics.median(latency) if latency else None,
        'latency_p95': _percentile(latency, 0.95) if latency else None,
        'known_costs': costs,
        'pending_cost_runs': pending,
        'bootstrap_pass_ci95': ([intervals[24], intervals[974]] if intervals else None),
    }


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = round((len(ordered) - 1) * fraction)
    return ordered[index]

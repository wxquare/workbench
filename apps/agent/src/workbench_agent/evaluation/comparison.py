"""Compare compatible evaluation reports and classify regressions."""

from __future__ import annotations


def compare_reports(baseline: dict, candidate: dict, policy: dict) -> dict:
    required = {
        'dataset_hash',
        'dataset_kind',
        'purpose',
        'split',
        'repeats',
        'scoring_version',
    }
    if any(baseline.get(key) != candidate.get(key) for key in required):
        return {
            'comparable': False,
            'status': 'incomparable',
            'reason': 'evaluation identity changed',
            'regressions': [],
        }
    if not baseline.get('metrics') or not candidate.get('metrics'):
        return {
            'comparable': False,
            'status': 'unavailable',
            'reason': 'evaluation metrics are missing',
            'regressions': [],
        }
    base_metrics = baseline.get('metrics') or {}
    candidate_metrics = candidate.get('metrics') or {}
    regression_reasons = []
    base_rate = base_metrics.get('rule_rate')
    cand_rate = candidate_metrics.get('rule_rate')
    if base_rate is not None and cand_rate is not None:
        if base_rate - cand_rate > policy.get('max_rule_rate_drop', 0.05):
            regression_reasons.append('rule_rate_regression')
    base_p95 = base_metrics.get('latency_p95')
    cand_p95 = candidate_metrics.get('latency_p95')
    if base_p95 and cand_p95 and cand_p95 > base_p95 * 1.25:
        regression_reasons.append('latency_regression')
    for key in ('unauthorized_accesses', 'incorrect_conclusions'):
        if (candidate_metrics.get(key) or 0) > (base_metrics.get(key) or 0):
            regression_reasons.append(key + '_regression')
    return {
        'comparable': True,
        'status': 'regressed' if regression_reasons else 'passed',
        'regressions': regression_reasons,
    }

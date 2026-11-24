"""Integer micro-unit budget reservations for evaluation experiments."""

from __future__ import annotations


def case_upper_bound(limits: dict, pricing: dict) -> int:
    currency = pricing.get('currency')
    version = pricing.get('pricing_version')
    input_price = pricing.get('input_micros_per_million')
    output_price = pricing.get('output_micros_per_million')
    if (
        not isinstance(currency, str)
        or len(currency) != 3
        or not version
        or type(input_price) is not int
        or type(output_price) is not int
        or input_price < 0
        or output_price < 0
    ):
        raise ValueError('known pricing is required for amount budgeting')
    calls = limits.get('model_calls')
    tokens = limits.get('tokens')
    if type(calls) is not int or type(tokens) is not int or calls < 1 or tokens < 1:
        raise ValueError('model and token limits are required')
    # Conservative: every token can receive the higher of input/output rates.
    return (calls * tokens * max(input_price, output_price) + 999_999) // 1_000_000


def reserve_case_budget(manifest: dict, usage: dict, upper_bound: dict) -> dict:
    budget = upper_bound.get('amount_micros')
    currency = upper_bound.get('currency')
    spent = usage.get('known_costs') or {}
    if type(budget) is not int or budget < 0:
        raise ValueError('unknown budget')
    if not isinstance(currency, str):
        raise ValueError('unknown budget currency')
    if any(key != currency for key in spent):
        raise ValueError('mixed budget currencies')
    used = spent.get(currency, 0)
    if usage.get('usage_pending') or usage.get('cost_pending'):
        used += manifest.get('reserved_upper_bound_micros', 0)
    next_bound = upper_bound.get('case_upper_bound_micros')
    if type(next_bound) is not int or next_bound < 0:
        raise ValueError('unknown case upper bound')
    allowed = used + next_bound <= budget
    return {
        'allowed': allowed,
        'currency': currency,
        'used_micros': used,
        'reserved_micros': next_bound if allowed else 0,
        'remaining_micros': max(0, budget - used - next_bound),
    }

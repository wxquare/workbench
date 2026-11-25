import pytest

from workbench_agent.evaluation.budget import case_upper_bound, reserve_case_budget


def test_unknown_price_is_rejected_and_upper_bound_is_conservative():
    with pytest.raises(ValueError):
        case_upper_bound({'model_calls': 1, 'tokens': 1}, {})
    value = case_upper_bound(
        {'model_calls': 2, 'tokens': 10},
        {
            'currency': 'USD',
            'pricing_version': 'v1',
            'input_micros_per_million': 1,
            'output_micros_per_million': 2,
        },
    )
    assert value == 4


def test_run_token_limit_is_a_total_not_a_per_call_limit():
    value = case_upper_bound(
        {'model_calls': 2, 'tokens': 1_000_000},
        {
            'currency': 'USD',
            'pricing_version': 'v1',
            'input_micros_per_million': 1,
            'output_micros_per_million': 2,
        },
    )
    assert value == 5  # Up to four input/output charges may round independently.


def test_upper_bound_covers_per_call_currency_rounding():
    value = case_upper_bound(
        {'model_calls': 2, 'tokens': 2},
        {
            'currency': 'USD',
            'pricing_version': 'v1',
            'input_micros_per_million': 1,
            'output_micros_per_million': 1,
        },
    )
    assert value == 2


def test_pending_usage_keeps_reservation_and_stops_before_overrun():
    usage = {'known_costs': {}, 'usage_pending': True, 'cost_pending': True}
    manifest = {'reserved_upper_bound_micros': 6}
    result = reserve_case_budget(
        manifest,
        usage,
        {
            'amount_micros': 10,
            'currency': 'USD',
            'case_upper_bound_micros': 5,
        },
    )
    assert result['allowed'] is False
    assert result['reserved_micros'] == 0


def test_mixed_currency_is_rejected():
    with pytest.raises(ValueError):
        reserve_case_budget(
            {},
            {'known_costs': {'CNY': 1}},
            {'amount_micros': 10, 'currency': 'USD', 'case_upper_bound_micros': 1},
        )

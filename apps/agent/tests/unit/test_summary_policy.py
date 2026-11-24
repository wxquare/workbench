import pytest

from workbench_agent.application.summary_policy import choose_summary_reserve
from workbench_agent.runtime.models.request_bound import request_token_bound


def test_default_reserve_tracks_bounded_input_and_output():
    assert (
        choose_summary_reserve(
            context_limit=64000,
            output_limit=8192,
            input_byte_limit=16384,
            message_limit=8,
            explicit=None,
        )
        == 26624
    )


def test_explicit_reserve_is_preserved():
    assert (
        choose_summary_reserve(
            context_limit=64000,
            output_limit=8192,
            input_byte_limit=16384,
            message_limit=8,
            explicit=30000,
        )
        == 30000
    )


@pytest.mark.parametrize(
    ('context_limit', 'output_limit', 'input_byte_limit', 'message_limit', 'explicit'),
    [
        (0, 8192, 16384, 8, None),
        (64000, 8192, -1, 8, None),
        (64000, 8192, 16384, 0, None),
        (64000, 8192, 16384, 8, 64001),
        (64000, 8192, 16384, 8, 0),
    ],
)
def test_invalid_summary_policy_is_rejected(
    context_limit,
    output_limit,
    input_byte_limit,
    message_limit,
    explicit,
):
    with pytest.raises(ValueError, match='SUMMARY_RESERVE_INVALID'):
        choose_summary_reserve(
            context_limit=context_limit,
            output_limit=output_limit,
            input_byte_limit=input_byte_limit,
            message_limit=message_limit,
            explicit=explicit,
        )


def test_request_bound_matches_conservative_utf8_envelope_formula():
    assert (
        request_token_bound(
            envelope={'messages': [{'content': '你好'}]},
            message_count=2,
            max_output_tokens=7,
        )
        == len('{"messages":[{"content":"你好"}]}'.encode('utf-8')) + 512 + 7
    )

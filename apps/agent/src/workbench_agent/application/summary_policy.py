"""Frozen summary reservation policy for newly admitted runs."""

SUMMARY_POLICY_VERSION = 'summary-reserve-v1'
SUMMARY_INPUT_BYTE_LIMIT = 16_384
SUMMARY_MESSAGE_LIMIT = 8


def choose_summary_reserve(
    *,
    context_limit: int,
    output_limit: int,
    input_byte_limit: int,
    message_limit: int,
    explicit: int | None,
) -> int:
    if context_limit <= 0 or output_limit <= 0 or input_byte_limit <= 0 or message_limit <= 0:
        raise ValueError('SUMMARY_RESERVE_INVALID')
    value = (
        explicit
        if explicit is not None
        else input_byte_limit + 256 * message_limit + output_limit
    )
    if value <= 0 or value > context_limit:
        raise ValueError('SUMMARY_RESERVE_INVALID')
    return value

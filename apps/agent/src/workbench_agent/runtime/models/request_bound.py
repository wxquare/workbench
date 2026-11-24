"""Conservative model request sizing shared with admission policy."""

import json


def request_token_bound(*, envelope: dict, message_count: int, max_output_tokens: int) -> int:
    return (
        len(json.dumps(envelope, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
        + 256 * message_count
        + max_output_tokens
    )

"""Canonical identities for explicitly approved external actions."""

import hashlib
import json


def action_payload_hash(tool_version: str, target: dict, arguments: dict) -> str:
    payload = {'tool_version': tool_version, 'target': target, 'arguments': arguments}
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()

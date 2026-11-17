import base64
import hashlib
import hmac
import json
from dataclasses import asdict


class SignedCursor:
    def __init__(self, secret, clock):
        if len(secret.encode()) < 32:
            raise ValueError("Cursor secret requires at least 32 bytes")
        self._secret, self.clock = secret.encode(), clock

    def _binding(self, principal_id, request):
        params = {k: v for k, v in request.parameters.items() if k != "cursor"}
        raw = json.dumps([principal_id, asdict(request.scope), params], sort_keys=True).encode()
        return hashlib.sha256(raw).hexdigest()

    def encode(self, principal_id, request, upstream_cursor):
        raw = json.dumps(
            {
                "binding": self._binding(principal_id, request),
                "cursor": upstream_cursor,
                "expires": self.clock.now().timestamp() + 300,
            },
            separators=(",", ":"),
        ).encode()
        body = base64.urlsafe_b64encode(raw).decode()
        signature = hmac.new(self._secret, body.encode(), hashlib.sha256).hexdigest()
        return f"{body}.{signature}"

    def decode(self, principal_id, request, token):
        try:
            body, signature = token.rsplit(".", 1)
            expected = hmac.new(self._secret, body.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError("Invalid cursor")
            value = json.loads(base64.urlsafe_b64decode(body))
            if (
                value["binding"] != self._binding(principal_id, request)
                or value["expires"] <= self.clock.now().timestamp()
                or not isinstance(value["cursor"], str)
            ):
                raise ValueError("Invalid cursor")
            return value["cursor"]
        except Exception:
            raise ValueError("Invalid or expired cursor") from None

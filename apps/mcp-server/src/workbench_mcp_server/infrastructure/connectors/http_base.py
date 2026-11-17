import json

import httpx

from workbench_mcp_server.application.results import GatewayError


class HttpGateway:
    def __init__(self, source, client, token, max_bytes=2097152):
        self.source, self.client = source, client
        self._token, self.max_bytes = token, max_bytes

    async def post(self, payload):
        try:
            async with self.client.stream(
                "POST",
                self.source.endpoint,
                json=payload,
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=httpx.Timeout(self.source.timeout_seconds, connect=3),
                follow_redirects=False,
            ) as response:
                if response.status_code != 200:
                    retry = response.status_code in {429, 502, 503, 504}
                    raise GatewayError("unavailable", "UPSTREAM_HTTP_ERROR", retry)
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise GatewayError("unknown", "UPSTREAM_RESPONSE_TOO_LARGE")
                    chunks.append(chunk)

                def invalid_constant(value):
                    raise ValueError("Nonfinite number")

                value = json.loads(b"".join(chunks), parse_constant=invalid_constant)
                if not isinstance(value, dict):
                    raise ValueError("Expected object")
                return value
        except httpx.TimeoutException:
            raise GatewayError("timeout", "UPSTREAM_TIMEOUT", True) from None
        except httpx.RequestError:
            raise GatewayError("unavailable", "UPSTREAM_CONNECTION_FAILED", True) from None
        except (ValueError, UnicodeError):
            raise GatewayError("unknown", "UPSTREAM_INVALID_JSON") from None

import json
import re
from dataclasses import asdict
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit

from mcp.types import CallToolResult, TextContent


class OutputMapper:
    def __init__(self, max_bytes, secrets, allowed_hosts, redact_fields=None):
        self.max_bytes = max_bytes
        self._secrets = sorted((s for s in secrets if s), key=len, reverse=True)
        self.allowed_hosts = set(allowed_hosts)
        self.redact_fields = {"token", "password", "authorization", "cookie", "secret"}
        self.redact_fields.update(field.lower() for field in (redact_fields or []))
        names = "|".join(re.escape(x) for x in self.redact_fields)
        quoted = r"""(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,;}]+)"""
        self.pattern = re.compile(
            rf"""(?i)["']?\b(?:{names})["']?\s*[:=]\s*{quoted}|\bBearer\s+[^\s,;]+"""
        )
        self.header_pattern = re.compile(
            r"""(?im)\b(?:authorization|proxy-authorization|cookie|set-cookie)["']?\s*[:=]\s*[^\r\n]+"""
        )

    def _clean(self, value, key=""):
        if key.lower() in self.redact_fields:
            return "[REDACTED]"
        if key == "source_uri" and value:
            try:
                parsed = urlsplit(value)
                if (
                    parsed.scheme != "https"
                    or parsed.hostname not in self.allowed_hosts
                    or parsed.username
                    or parsed.password
                ):
                    return None
                value = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
            except ValueError:
                return None
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, dict):
            return {k: self._clean(v, k) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._clean(v) for v in value]
        if isinstance(value, str):
            for secret in self._secrets:
                value = value.replace(secret, "[REDACTED]")
            value = self.header_pattern.sub("[REDACTED]", value)
            return self.pattern.sub("[REDACTED]", value)
        return value

    @staticmethod
    def _result(payload):
        return CallToolResult(
            content=[
                TextContent(
                    type="text", text=json.dumps(payload, ensure_ascii=False, allow_nan=False)
                )
            ],
            structuredContent=payload,
            isError=payload["status"] not in {"ok", "empty"},
        )

    def map(self, outcome):
        payload = self._clean(
            {
                "schema_version": "dod-tool-result-v1",
                "status": outcome.status,
                "data": outcome.data,
                "evidence_refs": [asdict(e) for e in outcome.evidence_refs],
                "artifacts": [],
                "error": outcome.error,
                "retryable": outcome.retryable,
                "meta": {"truncated": False, **outcome.meta},
            }
        )
        result = self._result(payload)
        while len(result.model_dump_json().encode()) > self.max_bytes:
            payload["meta"]["truncated"] = True
            removed = False
            for key in ("hypotheses", "guidance", "entries", "series", "spans", "matches"):
                records = payload["data"].get(key)
                if records:
                    del records[len(records) // 2 :]
                    if key == "entries":
                        payload["data"]["next_cursor"] = None
                    removed = True
                    break
            if "matches" in payload["data"]:
                ids = {r["source_id"] for r in payload["data"]["matches"]}
                payload["evidence_refs"] = [
                    e for e in payload["evidence_refs"] if e["source_locator"] in ids
                ]
            if not removed:
                payload.update(
                    status="unknown",
                    data={},
                    evidence_refs=[],
                    retryable=False,
                    error={"code": "RESULT_TOO_LARGE", "message": "Result exceeds output budget"},
                )
            result = self._result(payload)
        return result

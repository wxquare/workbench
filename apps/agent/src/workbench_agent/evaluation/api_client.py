"""Small public-API client used by baseline collection and replay tooling."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class APIError(Exception):
    status: int
    code: str
    retryable: bool = False

    def __str__(self):
        return f'API request failed with HTTP {self.status} ({self.code})'


class EvaluationAPI:
    def __init__(self, client: httpx.AsyncClient, *, token: str):
        if not token or token != token.strip():
            raise ValueError('token is required')
        self.client, self.token = client, token

    @property
    def headers(self) -> dict[str, str]:
        return {'Authorization': f'Bearer {self.token}'}

    @staticmethod
    def _error(response: httpx.Response) -> APIError:
        code = 'HTTP_ERROR'
        try:
            value = response.json()
            candidate = value.get('error', {}).get('code') if isinstance(value, dict) else None
            if isinstance(candidate, str) and candidate:
                code = candidate
        except (ValueError, TypeError):
            pass
        return APIError(
            response.status_code,
            code,
            retryable=response.status_code == 429 or response.status_code >= 500,
        )

    async def _json(
        self,
        method: str,
        path: str,
        *,
        body: dict | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict:
        response = await self.client.request(
            method,
            path,
            json=body,
            headers={**self.headers, **(headers or {})},
        )
        if not response.is_success:
            raise self._error(response)
        try:
            value = response.json()
        except ValueError:
            raise APIError(response.status_code, 'INVALID_JSON') from None
        if not isinstance(value, dict):
            raise APIError(response.status_code, 'INVALID_RESPONSE')
        return value

    async def create_run(self, body: dict, *, key: str) -> dict:
        if not key or key != key.strip():
            raise ValueError('idempotency key is required')
        return await self._json('POST', '/v1/runs', body=body, headers={'Idempotency-Key': key})

    async def create_kb_entry(self, body: dict, *, key: str) -> dict:
        return await self._json(
            'POST',
            '/v1/kb/entries',
            body=body,
            headers={'Idempotency-Key': key},
        )

    async def publish_kb_entry(
        self, entry_id: str, revision: int, *, key: str
    ) -> dict:
        return await self._json(
            'POST',
            f'/v1/kb/entries/{entry_id}/publish',
            headers={'Idempotency-Key': key, 'If-Match': str(revision)},
        )

    async def get_kb_entry(self, entry_id: str) -> dict:
        return await self._json('GET', f'/v1/kb/entries/{entry_id}')

    async def save_kb_entry(
        self, entry_id: str, body: dict, revision: int, *, key: str
    ) -> dict:
        return await self._json(
            'PATCH',
            f'/v1/kb/entries/{entry_id}',
            body=body,
            headers={'Idempotency-Key': key, 'If-Match': str(revision)},
        )

    async def get_kb_version(self, entry_id: str, version: int) -> dict:
        return await self._json(
            'GET', f'/v1/kb/entries/{entry_id}/versions/{version}'
        )

    async def search_knowledge(self, query: str, scope: dict, *, limit: int = 3) -> dict:
        return await self._json(
            'POST',
            '/v1/kb/search',
            body={'query': query, 'scope': scope, 'limit': limit},
        )

    async def get_run(self, run_id: str) -> dict:
        return await self._json('GET', f'/v1/runs/{run_id}')

    async def provenance(self, run_id: str) -> dict:
        headers = self.headers
        async with self.client.stream(
            'GET',
            f'/v1/runs/{run_id}/events',
            params={'after_seq': 0},
            headers=headers,
        ) as response:
            if not response.is_success:
                raise self._error(response)
            event_type = None
            data = []
            async for line in response.aiter_lines():
                if line.startswith('event:'):
                    event_type = line.partition(':')[2].strip()
                elif line.startswith('data:'):
                    data.append(line.partition(':')[2].strip())
                elif not line:
                    if event_type == 'run.accepted' and data:
                        try:
                            event = json.loads('\n'.join(data))
                            versions = event['payload']['versions']
                        except (ValueError, KeyError, TypeError):
                            raise APIError(response.status_code, 'INVALID_PROVENANCE') from None
                        if not isinstance(versions, dict):
                            raise APIError(response.status_code, 'INVALID_PROVENANCE')
                        return versions
                    event_type, data = None, []
            raise APIError(response.status_code, 'PROVENANCE_MISSING')

    async def evidence(self, run_id: str, evidence_id: str) -> dict:
        return await self._json('GET', f'/v1/runs/{run_id}/evidence/{evidence_id}')

    async def usage(self, run_id: str) -> dict:
        return await self._json('GET', f'/v1/usage?run_id={run_id}')

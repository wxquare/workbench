"""Shared atomic mutation receipts, usable by subsequent Run admission services."""
import hashlib
import json
from dataclasses import dataclass
from uuid import uuid4
from workbench_agent.application.errors import DomainError


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()

@dataclass(frozen=True)
class MutationResult:
    body: dict
    status: int = 200
    replayed: bool = False

class Idempotency:
    def __init__(self, backend):
        self.store, self.clock = backend.configuration, backend.clock

    async def lookup(self, uow, auth, agent_id, route, key, payload):
        """Return a committed receipt after caller has checked current authorization."""
        if not isinstance(key, str) or not 1 <= len(key) <= 128:
            raise DomainError('INVALID_IDEMPOTENCY_KEY', 422)
        encoded = canonical(payload)
        if len(encoded.encode('utf-8')) > 65536:
            raise DomainError('INPUT_TOO_LARGE', 413)
        resource = 'team_idempotency_records' if agent_id is None else 'idempotency_records'
        scope = dict(team_id=auth.team_id, subject=auth.subject, route=route, key=key)
        if agent_id is not None:
            scope['agent_id'] = agent_id
        previous = await self.store.one(uow, resource, **scope)
        if not previous or previous['expires_at'] <= await self.clock.now_ms(uow):
            return None
        if previous['request_hash'] != hashlib.sha256(encoded.encode('utf-8')).hexdigest():
            raise DomainError('IDEMPOTENCY_CONFLICT', 409)
        return MutationResult(json.loads(previous['response_ref']), previous['status_code'], True)

    async def execute(self, uow, auth, agent_id, route, key, payload, operation, status=200, *, max_payload_bytes=65536):
        return await self._execute(uow, auth, agent_id, route, key, payload, operation, status, max_payload_bytes=max_payload_bytes)

    async def execute_team(self, uow, auth, route, key, payload, operation, status=200, *, max_payload_bytes=65536):
        return await self._execute(uow, auth, None, route, key, payload, operation, status, max_payload_bytes=max_payload_bytes)

    async def _execute(self, uow, auth, agent_id, route, key, payload, operation, status, *, max_payload_bytes=65536):
        if not isinstance(key, str) or not 1 <= len(key) <= 128:
            raise DomainError('INVALID_IDEMPOTENCY_KEY', 422)
        encoded = canonical(payload)
        if max_payload_bytes is not None and len(encoded.encode('utf-8')) > max_payload_bytes:
            raise DomainError('INPUT_TOO_LARGE', 413)
        request_hash = hashlib.sha256(encoded.encode('utf-8')).hexdigest()
        resource = 'team_idempotency_records' if agent_id is None else 'idempotency_records'
        scope = dict(team_id=auth.team_id, subject=auth.subject, route=route, key=key)
        if agent_id is not None:
            scope['agent_id'] = agent_id
        previous = await self.store.one(uow, resource, **scope)
        now = await self.clock.now_ms(uow)
        if previous and previous['expires_at'] > now:
            if previous['request_hash'] != request_hash:
                raise DomainError('IDEMPOTENCY_CONFLICT', 409)
            return MutationResult(json.loads(previous['response_ref']), previous['status_code'], True)
        body = await operation()
        values = dict(request_hash=request_hash, response_ref=canonical(body), status_code=status,
            expires_at=now + 72 * 3600 * 1000, updated_at=now)
        if previous:
            await self.store.change(uow, resource, {'record_id': previous['record_id']}, values)
        else:
            await self.store.insert(uow, resource, dict(scope, record_id=str(uuid4()), created_at=now, **values))
        await self.store.insert(uow, 'audit_logs', dict(audit_id=str(uuid4()), team_id=auth.team_id,
            actor=auth.subject, object=agent_id if agent_id is not None else auth.team_id, operation=route, decision='allowed',
            trace=canonical({'request_hash': request_hash}), created_at=now, updated_at=now))
        return MutationResult(body, status)

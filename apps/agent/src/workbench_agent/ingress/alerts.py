"""Explicit synthetic alert protocol for local integration tests.

This is not a SeaTalk or production alert signature implementation. A deployment
must not expose this adapter as a real connector without a separately verified
protocol and binding. Authentication happens before parsing or admission.
"""

from workbench_agent.runtime.graphs.ids import GraphId
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import time
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from workbench_agent.domain.identity import AuthContext
from workbench_agent.domain.runs import CreateRun
from workbench_agent.application.errors import DomainError
from workbench_agent.application.runs import RunService


class Alert(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    event_id: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=65536)
    region: str = Field(min_length=1, max_length=64)
    occurred_at: int = Field(ge=0)


@dataclass(frozen=True)
class SyntheticAlertBinding:
    connector_id: str
    team_id: str
    agent_id: str
    actor_subject: str
    secret: bytes = field(repr=False)

    def __post_init__(self):
        if not self.connector_id or len(self.connector_id) > 128 or not self.secret:
            raise ValueError('invalid local test connector binding')


def mac_message(protocol, connector_id, timestamp, raw_body):
    """Length-prefixed fields prevent cross-protocol and connector substitution."""
    fields = (
        b'DOD-SYNTHETIC-MAC-V2',
        protocol.encode('utf-8'),
        connector_id.encode('utf-8'),
        timestamp.encode('ascii'),
        raw_body,
    )
    return b''.join(len(field).to_bytes(4, 'big') + field for field in fields)


def verify_signed(raw_body, headers, secret, *, protocol, connector_id):
    if not isinstance(raw_body, bytes) or len(raw_body) > 65536:
        raise DomainError('INVALID_INGRESS', 400)
    timestamp = headers.get('x-dod-timestamp', '')
    signature = headers.get('x-dod-signature', '')
    if (
        not isinstance(timestamp, str)
        or not timestamp.isascii()
        or not timestamp.isdigit()
        or len(timestamp) > 12
        or not isinstance(signature, str)
        or len(signature) != 64
        or any(ch not in '0123456789abcdef' for ch in signature)
    ):
        raise DomainError('INGRESS_AUTH_FAILED', 401)
    expected = hmac.new(
        secret, mac_message(protocol, connector_id, timestamp, raw_body), hashlib.sha256
    ).hexdigest()
    if abs(int(time.time()) - int(timestamp)) > 300 or not hmac.compare_digest(expected, signature):
        raise DomainError('INGRESS_AUTH_FAILED', 401)


class SyntheticAlertIngress:
    def __init__(self, backend, binding: SyntheticAlertBinding):
        self.binding = binding
        self.runs = RunService(backend)

    async def accept(self, raw_body: bytes, headers: dict):
        verify_signed(
            raw_body,
            headers,
            self.binding.secret,
            protocol='synthetic-alert-v1',
            connector_id=self.binding.connector_id,
        )
        try:
            event = Alert.model_validate_json(raw_body)
        except (ValidationError, ValueError):
            raise DomainError('INVALID_INGRESS', 422) from None
        now = int(time.time())
        if event.occurred_at < now - 7 * 24 * 3600:
            raise DomainError('INGRESS_BACKFILL_REQUIRED', 409)
        if event.occurred_at > now + 300:
            raise DomainError('INVALID_INGRESS', 422)
        binding = self.binding
        auth = AuthContext(
            team_id=binding.team_id,
            subject=binding.actor_subject,
            roles=(),
            identity_kind='connector',
            agent_scope=(binding.agent_id,),
        )
        # Separate connector IDs before hashing; API admission persists the event
        # receipt in the same transaction as the Run and its initial job.
        source_id = hashlib.sha256(
            json.dumps(
                [binding.connector_id, event.event_id], separators=(',', ':'), ensure_ascii=False
            ).encode()
        ).hexdigest()
        request = CreateRun(
            agent_id=binding.agent_id,
            mode='rca',
            input={'type': 'text', 'text': event.text},
            client_message_id=source_id,
            business_context={'region': event.region, 'source_occurred_at': event.occurred_at},
        )
        return await self.runs.create(
            auth, request, 'ingress:' + source_id, source_connector=binding.connector_id
        )


class AlertV2(Alert):
    service: str = Field(min_length=1, max_length=128)
    environment: str = Field(min_length=1, max_length=128)
    rule: str = Field(min_length=1, max_length=128)
    anchor: str = Field(min_length=1, max_length=64)
    window_seconds: int = Field(ge=1, le=3600)


@dataclass(frozen=True)
class SyntheticAlertV2Binding(SyntheticAlertBinding):
    identity_id: str = ''
    allowed_scopes: frozenset[tuple[str, str, str, str]] = frozenset()

    def __post_init__(self):
        super().__post_init__()
        if (
            not self.identity_id
            or not self.identity_id.strip()
            or self.identity_id != self.identity_id.strip()
            or len(self.identity_id) > 128
        ):
            raise ValueError('exact identity required')
        if not self.allowed_scopes or any(
            len(scope) != 4
            or any(not isinstance(v, str) or not v.strip() or v != v.strip() for v in scope)
            for scope in self.allowed_scopes
        ):
            raise ValueError('explicit synthetic scopes required')


class SyntheticAlertV2Ingress:
    """Local test protocol; it is not a production connector."""

    def __init__(self, backend, binding: SyntheticAlertV2Binding):
        self.backend, self.binding = backend, binding
        self.runs = RunService(backend)

    async def accept(self, raw_body: bytes, headers: dict):
        from sqlalchemy import insert, select
        from workbench_agent.persistence import schema
        from workbench_agent.application.idempotency import canonical, digest
        from workbench_agent.domain.query_scope import AlertScopeInput, resolve_alert_scope

        verify_signed(
            raw_body,
            headers,
            self.binding.secret,
            protocol='synthetic-alert-v2',
            connector_id=self.binding.connector_id,
        )
        try:
            event = AlertV2.model_validate_json(raw_body)
            if any(
                not value.strip() or value != value.strip()
                for value in (event.region, event.service, event.environment, event.rule)
            ):
                raise ValueError('invalid field')
            scope = resolve_alert_scope(
                AlertScopeInput(
                    team=self.binding.team_id,
                    service=event.service,
                    environment=event.environment,
                    body_region=event.region,
                    link_region=None,
                    anchor=event.anchor,
                    window_seconds=event.window_seconds,
                )
            ).scope
            if scope is None or abs(scope.anchor.timestamp() - event.occurred_at) > 300:
                raise ValueError('anchor mismatch')
        except (ValidationError, ValueError, TypeError, OverflowError):
            raise DomainError('INVALID_INGRESS', 422) from None
        now = int(time.time())
        if event.occurred_at < now - 7 * 24 * 3600:
            raise DomainError('INGRESS_BACKFILL_REQUIRED', 409)
        if event.occurred_at > now + 300:
            raise DomainError('INVALID_INGRESS', 422)
        if (
            event.service,
            event.environment,
            event.region,
            event.rule,
        ) not in self.binding.allowed_scopes:
            raise DomainError('OPERATION_DENIED', 403)
        binding = self.binding
        auth = AuthContext(
            team_id=binding.team_id,
            subject=binding.actor_subject,
            roles=(),
            identity_kind='connector',
            agent_scope=(binding.agent_id,),
        )
        source_id = hashlib.sha256(
            json.dumps(
                [binding.connector_id, event.event_id], separators=(',', ':'), ensure_ascii=False
            ).encode()
        ).hexdigest()
        request = CreateRun(
            agent_id=binding.agent_id,
            mode='rca',
            input={'type': 'text', 'text': event.text},
            client_message_id=source_id,
            business_context={
                'region': event.region,
                'service': event.service,
                'environment': event.environment,
                'anchor': event.anchor,
                'window_seconds': event.window_seconds,
                'source_occurred_at': event.occurred_at,
            },
        )
        payload = event.model_dump(mode='json')
        encoded = canonical(payload)
        from workbench_agent.application.runs import _SYNTHETIC_ALERT_V2

        async with self.backend.create_uow().begin_write() as uow:
            from workbench_agent.application.synthetic_source_policy import require_source_grant

            await require_source_grant(
                uow,
                {
                    'team_id': binding.team_id,
                    'agent_id': binding.agent_id,
                    'connector_id': binding.connector_id,
                    'identity_id': binding.identity_id,
                    'subject': binding.actor_subject,
                    'service': event.service,
                    'environment': event.environment,
                    'region': event.region,
                    'rule': event.rule,
                },
            )
            accepted = await self.runs.create(
                auth,
                request,
                'ingress:' + source_id,
                source_connector=binding.connector_id,
                _uow=uow,
                _ingress_capability=_SYNTHETIC_ALERT_V2,
            )
            run = await self.backend.runs.one(
                uow, 'runs', team_id=binding.team_id, run_id=str(accepted.run_id)
            )
            if json.loads(run['versions'])['graph_id'] not in (
                GraphId.RCA_SYNTHETIC,
                GraphId.RCA_PROPOSAL,
                GraphId.RCA_BATCH_PROPOSAL,
            ):
                raise DomainError('WORKFLOW_UNAVAILABLE', 409)
            table = schema.synthetic_alert_sources
            row = (
                (
                    await uow.execute(
                        select(table).where(
                            table.c.team_id == binding.team_id,
                            table.c.run_id == str(accepted.run_id),
                        )
                    )
                )
                .mappings()
                .first()
            )
            if row:
                if (
                    row['payload'] != encoded
                    or row['payload_hash'] != digest(payload)
                    or row['connector_id'] != binding.connector_id
                    or row['identity_id'] != binding.identity_id
                ):
                    raise DomainError('SOURCE_CONFLICT', 409)
            else:
                await uow.execute(
                    insert(table).values(
                        team_id=binding.team_id,
                        run_id=str(accepted.run_id),
                        connector_id=binding.connector_id,
                        payload=encoded,
                        payload_hash=digest(payload),
                        identity_id=binding.identity_id,
                        created_at=await self.backend.clock.now_ms(uow),
                    )
                )
            return accepted

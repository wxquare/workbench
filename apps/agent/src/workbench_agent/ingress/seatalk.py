"""Synthetic chat protocol; deliberately not the real SeaTalk protocol."""

from dataclasses import dataclass, field
import hashlib
import json
import time
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from workbench_agent.application.errors import DomainError
from workbench_agent.application.runs import RunService
from workbench_agent.domain.identity import AuthContext
from workbench_agent.domain.runs import CreateRun, ResumeRun
from uuid import UUID
from workbench_agent.ingress.alerts import verify_signed


class NamedResume(BaseModel):
    model_config = ConfigDict(extra='forbid')
    run_id: UUID
    interrupt_id: UUID
    expected_revision: int = Field(ge=0, strict=True)
    response: dict


class Chat(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    event_id: str = Field(min_length=1, max_length=128)
    thread_id: str = Field(min_length=1, max_length=128)
    actor: str = Field(min_length=1, max_length=128)
    text: str = Field(min_length=1, max_length=65536)
    occurred_at: int = Field(ge=0)
    resume: NamedResume | None = None


@dataclass(frozen=True)
class SyntheticChatBinding:
    connector_id: str
    team_id: str
    agent_id: str
    actors: dict[str, str]
    secret: bytes = field(repr=False)

    def __post_init__(self):
        if (
            not self.connector_id
            or len(self.connector_id) > 128
            or not self.secret
            or not self.actors
        ):
            raise ValueError('invalid synthetic chat binding')


class SyntheticChatIngress:
    def __init__(self, backend, binding):
        self.binding, self.runs = binding, RunService(backend)

    async def accept(self, raw_body, headers):
        verify_signed(
            raw_body,
            headers,
            self.binding.secret,
            protocol='synthetic-chat-v1',
            connector_id=self.binding.connector_id,
        )
        try:
            event = Chat.model_validate_json(raw_body)
        except (ValidationError, ValueError):
            raise DomainError('INVALID_INGRESS', 422) from None
        now = int(time.time())
        if event.occurred_at < now - 7 * 24 * 3600:
            raise DomainError('INGRESS_BACKFILL_REQUIRED', 409)
        if event.occurred_at > now + 300:
            raise DomainError('INVALID_INGRESS', 422)
        subject = self.binding.actors.get(event.actor)
        if not subject:
            raise DomainError('INGRESS_ACTOR_DENIED', 403)
        auth = AuthContext(
            team_id=self.binding.team_id,
            subject=subject,
            roles=(),
            identity_kind='connector',
            agent_scope=(self.binding.agent_id,),
        )
        event_key = hashlib.sha256(
            json.dumps(
                [self.binding.connector_id, event.event_id],
                ensure_ascii=False,
                separators=(',', ':'),
            ).encode()
        ).hexdigest()
        thread_key = hashlib.sha256(event.thread_id.encode()).hexdigest()
        if event.resume is not None:
            try:
                resume = ResumeRun.model_validate(event.resume.model_dump(exclude={'run_id'}))
            except ValidationError:
                raise DomainError('INVALID_INGRESS', 422) from None
            return await self.runs.resume(
                auth,
                str(event.resume.run_id),
                resume,
                'ingress:' + event_key,
                source_connector=self.binding.connector_id,
                source_thread=thread_key,
                source_event=event_key,
                source_payload=event.model_dump(mode='json'),
            )
        request = CreateRun(
            agent_id=self.binding.agent_id,
            mode='qa',
            input={'type': 'text', 'text': event.text},
            client_message_id=event_key,
            business_context={
                'source_occurred_at': event.occurred_at,
                'source_actor': event.actor,
                'source_thread': thread_key,
            },
        )
        return await self.runs.create(
            auth,
            request,
            'ingress:' + event_key,
            source_connector=self.binding.connector_id,
            source_thread=thread_key,
        )

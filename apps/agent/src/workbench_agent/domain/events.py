"""Version 1 public event vocabulary and wire envelope."""

import json
from typing import Literal
from dataclasses import dataclass
from pydantic import Field
from workbench_agent.domain.profiles import FrozenModel

EventType = Literal[
    'run.accepted',
    'run.started',
    'run.status_changed',
    'phase.changed',
    'skill.loaded',
    'tool.started',
    'tool.completed',
    'evidence.added',
    'assistant.delta',
    'assistant.message_superseded',
    'input.required',
    'approval.required',
    'run.resumed',
    'run.recovery_scheduled',
    'run.completed',
    'run.failed',
    'run.cancelled',
    'run.expired',
    'action.updated',
    'delivery.updated',
]


class RunEvent(FrozenModel):
    schema_version: Literal['1'] = '1'
    run_id: str
    seq: int = Field(ge=1)
    type: EventType
    occurred_at: str
    attempt: int = Field(ge=0)
    message_id: str | None = None
    payload: dict

    def to_sse(self):
        body = json.dumps(
            self.model_dump(mode='json'),
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        )
        return f'id: {self.run_id}:{self.seq}\nevent: {self.type}\ndata: {body}\n\n'.encode()


@dataclass(frozen=True)
class ActionGuard:
    team_id: str
    run_id: str
    action_id: str
    outbox_id: str
    worker_id: str
    revision: int

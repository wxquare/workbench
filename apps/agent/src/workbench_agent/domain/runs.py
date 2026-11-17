"""Immutable admission DTOs. Runtime execution is deliberately a separate boundary."""
from datetime import datetime, timezone
from typing import Literal
from uuid import UUID
from pydantic import Field
from workbench_agent.domain.profiles import FrozenModel, ProfileBudgetLimits
from workbench_agent.domain.context import RunContext

Mode = Literal['auto', 'qa', 'rca', 'code', 'sop', 'inspection']

BudgetLimits = ProfileBudgetLimits

class RunInput(FrozenModel):
    type: Literal['text'] = 'text'
    text: str = Field(min_length=1)
    artifact_ids: tuple[UUID, ...] = ()

class CreateThread(FrozenModel):
    agent_id: UUID

class CreateRun(FrozenModel):
    agent_id: UUID
    thread_id: UUID | None = None
    mode: Mode = 'auto'
    input: RunInput
    business_context: dict = Field(default_factory=dict)
    response_language: str = Field(default='zh-CN', min_length=1, max_length=64)
    client_message_id: str | None = Field(default=None, min_length=1, max_length=128)
    workflow_version_id: UUID | None = None
    budgets: BudgetLimits = Field(default_factory=BudgetLimits)
    summary_token_reserve: int | None = Field(default=None,gt=0,strict=True,exclude_if=lambda value:value is None)
    expected_revision: int | None = Field(default=None, ge=0, strict=True)

class AcceptedRun(FrozenModel):
    run_id: str
    thread_id: str
    status: str = 'queued'
    revision: int = 1
    accepted_at: str
    links: dict[str, str]
    replayed: bool = Field(default=False, exclude=True)

class ThreadView(FrozenModel):
    thread_id: str
    agent_id: str
    active_run_id: str | None
    revision: int
    created_at: str

class MessageView(FrozenModel):
    message_id: str
    thread_id: str
    run_id: str | None
    role: str
    input: RunInput | None = None
    outcome: dict | None = None
    created_at: str

class RunView(FrozenModel):
    run_id: str
    thread_id: str
    agent_id: str
    status: str
    revision: int
    phase: str | None
    attempt: int = 0
    created_at: str
    started_at: str | None = None
    active_elapsed_ms: int = 0
    waiting: dict | None = None
    outcome: dict | None = None
    usage: dict = Field(default_factory=dict)
    delivery_summary: dict = Field(default_factory=dict)
    links: dict[str, str]
    context: RunContext = Field(exclude=True)

def timestamp(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')

def run_links(run_id):
    return {'self': f'/v1/runs/{run_id}', 'events': f'/v1/runs/{run_id}/events'}

class UserInputResponse(FrozenModel):
    kind: Literal['user_input']
    values: dict = Field(default_factory=dict)
    text: str | None = None

class ApprovalResponse(FrozenModel):
    kind: Literal['approval']
    approval_id: str = Field(min_length=1,max_length=128)
    decision: Literal['approve','reject']
    expected_args_hash: str = Field(min_length=1,max_length=128)

class ResumeRun(FrozenModel):
    interrupt_id: UUID
    expected_revision: int = Field(ge=0,strict=True)
    response: UserInputResponse | ApprovalResponse = Field(discriminator='kind')

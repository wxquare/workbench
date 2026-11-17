"""Immutable per-network-attempt budget receipts; retries use distinct invocation IDs."""
from typing import Literal
from pydantic import Field, model_validator
from workbench_agent.domain.profiles import FrozenModel

class ReservationRequest(FrozenModel):
    invocation_id: str = Field(min_length=1,max_length=128)
    kind: Literal['model','expert','evaluation','summary','tool']
    model_attempts: int = Field(default=0,ge=0,le=1,strict=True)
    tool_attempts: int = Field(default=0,ge=0,le=1,strict=True)
    token_upper_bound: int = Field(default=0,ge=0,strict=True)
    role: Literal['router','knowledge','diagnosis','code','summary','evaluation'] = 'diagnosis'

    @model_validator(mode='after')
    def one_network_attempt(self):
        if self.model_attempts+self.tool_attempts != 1:
            raise ValueError('one reservation represents exactly one actual network attempt')
        if (self.kind=='tool') != bool(self.tool_attempts):
            raise ValueError('attempt kind does not match reservation')
        if self.model_attempts and self.token_upper_bound==0:
            raise ValueError('model attempts require a positive token bound')
        return self

class ActualUsage(FrozenModel):
    input_tokens: int | None = Field(default=None,ge=0,strict=True)
    output_tokens: int | None = Field(default=None,ge=0,strict=True)
    amount_micros: int | None = Field(default=None,ge=0,strict=True)
    currency: str | None = Field(default=None,pattern=r'^[A-Z]{3}$')
    pricing_version: str | None = Field(default=None,min_length=1,max_length=128)

    @model_validator(mode='after')
    def cost_receipt(self):
        if self.amount_micros is not None and (self.currency is None or self.pricing_version is None):
            raise ValueError('known cost requires currency and pricing version')
        return self

class Reservation(FrozenModel):
    reservation_id: str
    invocation_id: str
    kind: str
    status: str
    send_state: str
    model_attempts: int
    tool_attempts: int
    token_upper_bound: int
    dispatch_allowed: bool = False

class UsageRecord(ActualUsage):
    record_id: str
    invocation_id: str
    usage_pending: bool
    cost_pending: bool

class UsageSummary(FrozenModel):
    summary_token_reserve: int | None = None
    model_attempts: int = 0
    tool_attempts: int = 0
    reserved_model_attempts: int = 0
    reserved_tool_attempts: int = 0
    reserved_tokens: int = 0
    known_tokens: int = 0
    input_tokens: int | None = 0
    output_tokens: int | None = 0
    total_tokens: int | None = 0
    amount_micros: int | None = 0
    currency: str | None = None
    known_costs: dict[str,int] = Field(default_factory=dict)
    usage_pending: bool = False
    cost_pending: bool = False
    active_elapsed_ms: int = 0
    token_overage: int = 0

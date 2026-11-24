"""Validated public results; foundation recording is explicitly not a diagnosis."""

from hashlib import sha256
import json
from typing import Literal
from uuid import UUID
from pydantic import Field, model_validator
from workbench_agent.domain.profiles import FrozenModel
from workbench_agent.domain.knowledge_answers import KnowledgeAnswer
from workbench_agent.domain.tools import WebSearchSource


class Finding(FrozenModel):
    summary: str = Field(min_length=1, max_length=16384)
    evidence_refs: tuple[UUID, ...] = Field(min_length=1)
    status: Literal['observed', 'normal', 'uncertain'] = 'observed'


class Hypothesis(FrozenModel):
    summary: str = Field(min_length=1, max_length=16384)
    evidence_refs: tuple[UUID, ...] = Field(min_length=1)
    status: Literal['unverified', 'supported', 'rejected'] = 'unverified'


class Impact(FrozenModel):
    summary: str = Field(min_length=1, max_length=16384)
    evidence_refs: tuple[UUID, ...] = Field(min_length=1)


class ProposedAction(FrozenModel):
    summary: str = Field(min_length=1, max_length=16384)
    # Suggestions are not executions, confirmations, or delivery receipts.
    evidence_refs: tuple[UUID, ...] = ()


class UsageSummary(FrozenModel):
    status: Literal['pending', 'complete'] = 'pending'
    model_calls: int = Field(default=0, ge=0, strict=True)
    tool_calls: int = Field(default=0, ge=0, strict=True)
    input_tokens: int | None = Field(default=None, ge=0, strict=True)
    output_tokens: int | None = Field(default=None, ge=0, strict=True)
    amount_micros: int | None = Field(default=None, ge=0, strict=True)
    currency: str | None = Field(default=None, pattern=r'^[A-Z]{3}$')

    @model_validator(mode='after')
    def amounts(self):
        if (self.amount_micros is None) != (self.currency is None):
            raise ValueError('amount and currency required together')
        if self.status == 'complete' and any(
            x is None for x in (self.input_tokens, self.output_tokens, self.amount_micros)
        ):
            raise ValueError('unknown usage must remain pending')
        return self


class ResultDiagnostics(FrozenModel):
    retrieval_truncated: bool = False
    structured_output_repairs: int = Field(default=0, ge=0, le=1, strict=True)
    structured_output_failed: bool = False


class RunOutcome(FrozenModel):
    schema_version: Literal['1'] = '1'
    kind: Literal['foundation.recorded.v1', 'analysis.v1']
    completed: bool = Field(strict=True)
    summary: str = Field(min_length=1, max_length=65536)
    findings: tuple[Finding, ...]
    root_cause_hypotheses: tuple[Hypothesis, ...]
    impact: Impact | None
    evidence_refs: tuple[UUID, ...]
    unresolved_items: tuple[str, ...]
    proposed_actions: tuple[ProposedAction, ...]
    knowledge_gaps: tuple[str, ...]
    usage_summary: UsageSummary
    diagnostics: ResultDiagnostics = Field(
        default_factory=ResultDiagnostics,
        exclude_if=lambda value: value == ResultDiagnostics(),
    )
    web_search_sources: tuple[WebSearchSource, ...] = Field(
        default=(),
        exclude_if=lambda value: not value,
    )
    knowledge_answer: KnowledgeAnswer | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode='after')
    def completeness(self):
        if not self.summary.strip():
            raise ValueError('empty summary')
        if not self.completed and not any(x.strip() for x in self.unresolved_items):
            raise ValueError('incomplete result requires unresolved work')
        if self.completed and self.unresolved_items:
            raise ValueError('completed result cannot have unresolved work')
        refs = set(self.evidence_refs)
        if self.knowledge_answer:
            if self.kind != 'analysis.v1':
                raise ValueError('knowledge answer requires analysis result')
            cited = {
                citation.evidence_ref
                for claim in self.knowledge_answer.claims
                for citation in claim.citations
            }
            if not cited <= refs:
                raise ValueError('unlisted knowledge evidence reference')
            if self.completed and self.knowledge_answer.verification != 'passed':
                raise ValueError('completed answer requires verification')
        for value in (
            *self.findings,
            *self.root_cause_hypotheses,
            *self.proposed_actions,
            *((self.impact,) if self.impact else ()),
        ):
            if not set(value.evidence_refs) <= refs:
                raise ValueError('unlisted evidence reference')
        if self.kind == 'foundation.recorded.v1' and (
            self.findings
            or self.root_cause_hypotheses
            or self.impact
            or self.evidence_refs
            or self.proposed_actions
        ):
            raise ValueError('foundation recording cannot claim business analysis')
        if len(self.canonical().encode()) > 2 * 1024 * 1024:
            raise ValueError('outcome too large')
        return self

    def canonical(self):
        return json.dumps(
            self.model_dump(mode='json'),
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        )

    def digest(self):
        return sha256(self.canonical().encode()).hexdigest()

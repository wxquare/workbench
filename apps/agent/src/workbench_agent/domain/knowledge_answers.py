"""Structured knowledge answers distinguish exact citation and model review."""
from typing import Literal
from uuid import UUID
from pydantic import Field, model_validator
from workbench_agent.domain.profiles import FrozenModel


class CitationDraft(FrozenModel):
    evidence_ref: UUID
    source_id: str = Field(min_length=1, max_length=512)
    quote: str = Field(min_length=1, max_length=1500)


class ClaimDraft(FrozenModel):
    text: str = Field(min_length=1, max_length=2048)
    citations: tuple[CitationDraft, ...] = Field(min_length=1, max_length=4)


class AnswerDraft(FrozenModel):
    answerable: bool = Field(strict=True)
    claims: tuple[ClaimDraft, ...] = Field(default=(), max_length=8)
    missing: tuple[str, ...] = Field(default=(), max_length=8)

    @model_validator(mode='after')
    def coherent_answer(self):
        if self.answerable != bool(self.claims) or self.answerable == bool(self.missing):
            raise ValueError('answerable requires claims; unanswerable requires missing information')
        if any(not claim.text.strip() or any(not c.quote.strip() for c in claim.citations) for claim in self.claims):
            raise ValueError('claims and quotations must be nonempty')
        if any(not item.strip() or len(item) > 512 for item in self.missing):
            raise ValueError('invalid missing information')
        return self


class SourceCitation(CitationDraft):
    title: str = Field(max_length=4000)
    source_locator: str | None = None
    source_uri: str | None = None


class ValidatedClaim(FrozenModel):
    text: str = Field(min_length=1, max_length=2048)
    citations: tuple[SourceCitation, ...] = Field(min_length=1, max_length=4)


class ValidatedAnswer(FrozenModel):
    claims: tuple[ValidatedClaim, ...] = Field(min_length=1, max_length=8)


class SupportCheck(FrozenModel):
    index: int = Field(ge=0, strict=True)
    supported: bool = Field(strict=True)
    reason: str = Field(max_length=1024)


class SupportReview(FrozenModel):
    checks: tuple[SupportCheck, ...] = Field(min_length=1, max_length=8)

    def approves(self, count: int) -> bool:
        return {item.index for item in self.checks} == set(range(count)) and len(self.checks) == count and all(item.supported for item in self.checks)


class KnowledgeAnswer(ValidatedAnswer):
    schema_version: Literal['knowledge-answer-v1'] = 'knowledge-answer-v1'
    review_kind: Literal['model_reviewed'] = 'model_reviewed'
    verification: Literal['passed', 'failed']

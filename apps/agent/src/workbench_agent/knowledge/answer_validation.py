"""Bind exact quotations to already-authorized evidence, never model-supplied URLs."""

from pydantic import Field
from datetime import datetime, timedelta
from workbench_agent.domain.profiles import FrozenModel
from workbench_agent.domain.knowledge_answers import (
    AnswerDraft,
    SourceCitation,
    ValidatedAnswer,
    ValidatedClaim,
)


class CitationValidationError(ValueError):
    pass


class SourcePassage(FrozenModel):
    evidence_ref: str = Field(min_length=1, max_length=128)
    source_id: str = Field(min_length=1, max_length=512)
    content: str = Field(max_length=100_000)
    title: str = Field(max_length=4000)
    source_locator: str | None = None
    source_uri: str | None = None
    updated_at: str | None = None


def collect_passages(payloads):
    result = {}
    for ref, payload in payloads.items():
        if (
            not isinstance(payload, dict)
            or payload.get('schema_version') != 'dod-tool-result-v1'
            or payload.get('status') != 'ok'
        ):
            continue
        data = payload.get('data')
        if not isinstance(data, dict) or not isinstance(data.get('matches'), list):
            continue
        for item in data['matches'][:10]:
            if not isinstance(item, dict):
                continue
            try:
                source = SourcePassage(
                    evidence_ref=str(ref),
                    source_id=item['source_id'],
                    content=item['content'],
                    title=item['title'],
                    source_locator=item.get('source_locator'),
                    source_uri=item.get('source_uri'),
                    updated_at=item.get('updated_at'),
                )
            except (ValueError, KeyError, TypeError):
                raise CitationValidationError('INVALID_SOURCE') from None
            key = (source.evidence_ref, source.source_id)
            if key in result and result[key] != source:
                raise CitationValidationError('CONFLICTING_SOURCE')
            result[key] = source
    return result


def validate_answer(draft: AnswerDraft, sources) -> ValidatedAnswer:
    if not draft.answerable:
        raise CitationValidationError('UNANSWERABLE')
    claims = []
    for claim in draft.claims:
        citations = []
        for citation in claim.citations:
            source = sources.get((str(citation.evidence_ref), citation.source_id))
            if source is None or not citation.quote.strip() or citation.quote not in source.content:
                raise CitationValidationError('CITATION_NOT_IN_SOURCE')
            citations.append(
                SourceCitation(
                    **citation.model_dump(),
                    title=source.title,
                    source_locator=source.source_locator,
                    source_uri=source.source_uri,
                )
            )
        claims.append(ValidatedClaim(text=claim.text, citations=tuple(citations)))
    return ValidatedAnswer(claims=tuple(claims))


def validate_freshness(answer, sources, *, now, max_age_seconds):
    if (
        type(max_age_seconds) is not int
        or max_age_seconds <= 0
        or not isinstance(now, datetime)
        or now.tzinfo is None
        or now.utcoffset() is None
    ):
        raise CitationValidationError('FRESHNESS_UNAVAILABLE')
    for claim in answer.claims:
        for citation in claim.citations:
            source = sources.get((str(citation.evidence_ref), citation.source_id))
            try:
                timestamp = datetime.fromisoformat(source.updated_at.replace('Z', '+00:00'))
                if (
                    timestamp.tzinfo is None
                    or timestamp.utcoffset() is None
                    or timestamp > now
                    or now - timestamp > timedelta(seconds=max_age_seconds)
                ):
                    raise ValueError()
            except (AttributeError, TypeError, ValueError, OverflowError):
                raise CitationValidationError('FRESHNESS_UNAVAILABLE') from None

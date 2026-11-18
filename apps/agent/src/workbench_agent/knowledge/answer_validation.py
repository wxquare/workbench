"""Bind exact quotations to already-authorized evidence, never model-supplied URLs."""
from pydantic import Field
from workbench_agent.domain.profiles import FrozenModel
from workbench_agent.domain.knowledge_answers import AnswerDraft, SourceCitation, ValidatedAnswer, ValidatedClaim


class CitationValidationError(ValueError):
    pass


class SourcePassage(FrozenModel):
    evidence_ref: str = Field(min_length=1, max_length=128)
    source_id: str = Field(min_length=1, max_length=512)
    content: str = Field(max_length=100_000)
    title: str = Field(max_length=4000)
    source_locator: str | None = None
    source_uri: str | None = None


def collect_passages(payloads):
    result = {}
    for ref, payload in payloads.items():
        if not isinstance(payload, dict) or payload.get('schema_version') != 'dod-tool-result-v1' or payload.get('status') != 'ok':
            continue
        data = payload.get('data')
        if not isinstance(data, dict) or not isinstance(data.get('matches'), list):
            continue
        for item in data['matches'][:10]:
            if not isinstance(item, dict):
                continue
            try:
                source = SourcePassage(evidence_ref=str(ref), source_id=item['source_id'], content=item['content'],
                    title=item['title'], source_locator=item.get('source_locator'), source_uri=item.get('source_uri'))
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
            citations.append(SourceCitation(**citation.model_dump(), title=source.title,
                source_locator=source.source_locator, source_uri=source.source_uri))
        claims.append(ValidatedClaim(text=claim.text, citations=tuple(citations)))
    return ValidatedAnswer(claims=tuple(claims))

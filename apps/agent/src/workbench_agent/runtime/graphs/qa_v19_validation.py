"""Validate citations against the exact authorized source used for synthesis.

A matching quotation establishes provenance, not semantic entailment. Callers must
supply currently authorized evidence and propagate extraction failures as partial.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
import re
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field


class AnswerClaim(BaseModel):
    model_config = ConfigDict(extra='forbid')
    text: str = Field(min_length=1, max_length=500)
    evidence_ref: str = Field(min_length=1, max_length=128)
    source_id: str = Field(min_length=1, max_length=512)
    quote: str = Field(min_length=1, max_length=1024)


class CitedAnswer(BaseModel):
    model_config = ConfigDict(extra='forbid')
    answerable: bool
    claims: list[AnswerClaim] = Field(max_length=6)
    unanswered: list[str] = Field(max_length=5)
    conflicts: list[str] = Field(max_length=5)


@dataclass(frozen=True)
class KnowledgeSource:
    evidence_ref: str
    source_id: str
    content: str
    updated_at: datetime | None
    source_uri: str | None


@dataclass(frozen=True)
class ExtractionResult:
    sources: list[KnowledgeSource]
    rejection_reasons: list[str]


@dataclass(frozen=True)
class VerificationResult:
    accepted: list[tuple[AnswerClaim, KnowledgeSource]]
    rejection_reasons: list[str]
    completed: bool


def extract_knowledge_sources(evidence_ref: str, payload: object) -> ExtractionResult:
    """Extract bounded source texts from an already authorized tool result.

    Do not use this as an authorization check. A bad match invalidates the whole
    payload so that it cannot be mistaken for a complete search result.
    """
    def invalid(reason):
        return ExtractionResult([], [reason])

    if not isinstance(evidence_ref, str) or not evidence_ref.strip():
        return invalid('Invalid evidence reference.')
    if not isinstance(payload, dict) or payload.get('schema_version') != 'dod-tool-result-v1' or payload.get('status') != 'ok':
        return invalid('Invalid tool result.')
    data = payload.get('data')
    meta = payload.get('meta', {})
    if (not isinstance(data, dict) or not isinstance(meta, dict) or payload.get('truncated')
            or data.get('truncated') or meta.get('truncated')):
        return invalid('Missing or truncated tool result.')
    matches = data.get('matches')
    if not isinstance(matches, list) or not matches or len(matches) > 10:
        return invalid('Missing or excessive source matches.')
    sources, ids = [], set()
    for item in matches:
        if not isinstance(item, dict):
            return invalid('Invalid source match.')
        source_id, content = item.get('source_id'), item.get('content')
        if (not isinstance(source_id, str) or not source_id.strip() or len(source_id) > 512
                or source_id in ids or not isinstance(content, str) or not content.strip() or len(content) > 4096):
            return invalid('Invalid, duplicate, or oversized source match.')
        ids.add(source_id)
        raw_time = item.get('updated_at')
        try:
            updated_at = datetime.fromisoformat(raw_time.replace('Z', '+00:00')) if isinstance(raw_time, str) else None
        except ValueError:
            updated_at = None
        uri = item.get('source_uri')
        sources.append(KnowledgeSource(evidence_ref, source_id, content, updated_at, uri if isinstance(uri, str) else None))
    return ExtractionResult(sources, [])


def _normalized(value: str) -> str:
    return ' '.join(value.split())


# Numbers include signs, decimals, versions, dates and percentages. Boundaries
# prevent e.g. 2.5 from matching -2.5, or 80% from matching 180%.
_TOKENS = re.compile(r'`[^`]+`|(?<![A-Za-z0-9_])[vV]\d+(?:\.\d+)+|(?<![0-9.])[+-]?(?:\d+(?:[.\-/]\d+)*|\.\d+)%?|(?<![A-Za-z0-9_])[A-Z][A-Z0-9_]{1,}(?![A-Za-z0-9_])|(?<![A-Za-z0-9_])/(?:[^\s`]+)')


def _supported_tokens(text: str, content: str) -> bool:
    source_tokens = set(_TOKENS.findall(content))
    return all(token in source_tokens for token in _TOKENS.findall(text))


def _safe_uri(value: object) -> bool:
    if (not isinstance(value, str) or not value or any(c.isspace() or ord(c) < 32 for c in value)
            or '?' in value or '#' in value or '\\' in value):
        return False
    try:
        parts = urlsplit(value)
        return (parts.scheme == 'https' and bool(parts.hostname) and parts.username is None
                and parts.password is None and parts.port != 0)
    except ValueError:
        return False


def _aware(value):
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


def verify_answer(answer: CitedAnswer, sources: list[KnowledgeSource], *, now: datetime, max_age: timedelta | None) -> VerificationResult:
    reasons, accepted = [], []
    if not answer.answerable:
        reasons.append('Answer is not answerable.')
    if not answer.claims:
        reasons.append('Answer contains no claims.')
    if answer.unanswered:
        reasons.append('Answer has unanswered questions.')
    if answer.conflicts:
        reasons.append('Answer has conflicts.')
    if not _aware(now) or not isinstance(max_age, timedelta) or max_age <= timedelta(0):
        reasons.append('Freshness cannot be determined.')
    by_key, duplicate_ids = {}, set()
    ids = set()
    for source in sources:
        if source.source_id in ids:
            duplicate_ids.add(source.source_id)
        ids.add(source.source_id)
        by_key[(source.evidence_ref, source.source_id)] = source
    seen = set()
    for claim in answer.claims:
        key = (claim.evidence_ref, claim.source_id)
        reference = (claim.evidence_ref, claim.source_id, _normalized(claim.quote))
        if reference in seen:
            reasons.append('Duplicate citation.')
            continue
        seen.add(reference)
        source = by_key.get(key)
        if not source or claim.source_id in duplicate_ids:
            reasons.append('Unknown or ambiguous source.')
            continue
        if (not claim.text.strip() or not claim.quote.strip() or not isinstance(source.content, str)
                or len(source.content) > 4096 or _normalized(claim.quote) not in _normalized(source.content)):
            reasons.append('Quote is absent from source.')
            continue
        if not _safe_uri(source.source_uri):
            reasons.append('Source URI is missing or unsafe.')
            continue
        if not _supported_tokens(claim.text, source.content):
            reasons.append('Claim contains unsupported numbers or identifiers.')
            continue
        if (not _aware(now) or not _aware(source.updated_at) or not isinstance(max_age, timedelta)
                or max_age <= timedelta(0) or source.updated_at > now or now - source.updated_at > max_age):
            reasons.append('Source freshness cannot be verified.')
            continue
        accepted.append((claim, source))
    return VerificationResult(accepted, reasons, not reasons and len(accepted) == len(answer.claims))

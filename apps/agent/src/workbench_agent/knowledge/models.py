"""Bounded, immutable knowledge content contracts."""

from datetime import date
from hashlib import sha256
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from workbench_agent.application.idempotency import canonical
from workbench_agent.knowledge.chunking import normalize_markdown


class KnowledgeValue(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')


class KBDraft(KnowledgeValue):
    title: str = Field(min_length=1, max_length=200)
    kind: Literal['faq', 'runbook', 'glossary', 'case']
    body_md: str
    service: str | None = Field(default=None, max_length=128)
    environment: str | None = Field(default=None, max_length=128)
    region: str | None = Field(default=None, max_length=128)
    tags: tuple[str, ...] = Field(default=(), max_length=16)
    aliases: tuple[str, ...] = Field(default=(), max_length=16)
    review_by: date | None = None

    @field_validator('title')
    @classmethod
    def validate_title(cls, value):
        value = value.strip()
        if not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError('invalid title')
        return value

    @field_validator('body_md')
    @classmethod
    def validate_body(cls, value):
        return normalize_markdown(value.encode('utf-8'))

    @field_validator('service', 'environment', 'region')
    @classmethod
    def validate_scope(cls, value):
        if value is not None and (
            not value.strip()
            or value != value.strip()
            or value.casefold() == 'all'
            or ',' in value
            or '*' in value
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
        ):
            raise ValueError('invalid scope')
        return value

    @field_validator('tags', 'aliases')
    @classmethod
    def validate_names(cls, values):
        if any(
            not v.strip()
            or v != v.strip()
            or len(v) > 64
            or any(ord(c) < 32 or ord(c) == 127 for c in v)
            for v in values
        ):
            raise ValueError('invalid tag or alias')
        return values


class KBScope(KnowledgeValue):
    team_id: str
    service: str
    environment: str
    region: str


class GapDraftTemplate(KnowledgeValue):
    title: str = Field(min_length=1, max_length=200)
    service: str | None = None
    environment: str | None = None
    region: str | None = None
    gap_id: str
    run_id: str


class KBChunk(KnowledgeValue):
    chunk_id: str
    heading_path: tuple[str, ...]
    line_start: int
    line_end: int
    content: str
    content_hash: str
    chunker_version: str


class SearchHit(KnowledgeValue):
    entry_id: str
    version: int
    chunk: KBChunk
    title: str
    kind: Literal['faq', 'runbook', 'glossary', 'case']
    updated_at: int
    source_id: str


def draft_hash(draft: KBDraft) -> str:
    return sha256(canonical(draft.model_dump(mode='json')).encode('utf-8')).hexdigest()

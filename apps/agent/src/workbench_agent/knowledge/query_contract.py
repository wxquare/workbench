"""Strict normalized-query-v1 boundary and KnowledgeData response."""
from datetime import datetime, timezone
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from typing import Literal

from workbench_agent.application.errors import DomainError


class QueryModel(BaseModel):
    model_config = ConfigDict(extra='forbid', hide_input_in_errors=True)


class QueryScope(QueryModel):
    team: str = Field(min_length=1, max_length=128)
    source_id: str = Field(min_length=1, max_length=128)
    service: str = Field(min_length=1, max_length=128)
    environment: str = Field(min_length=1, max_length=128)
    region: str = Field(min_length=1, max_length=128)


class Parameters(QueryModel):
    query: str = Field(min_length=1, max_length=4000)
    scene: Literal['qa', 'alert']
    limit: int = Field(ge=1, le=10, strict=True)


class Query(QueryModel):
    schema_version: Literal['normalized-query-v1']
    tool: Literal['knowledge_search']
    scope: QueryScope
    parameters: Parameters


def normalize_query(raw):
    try:
        query = Query.model_validate(raw)
    except ValidationError:
        raise DomainError('KB_INVALID_CONTENT', 422) from None
    return query.scope.model_dump(), query.parameters.query, query.parameters.scene, query.parameters.limit


def knowledge_data(hits, *, public_base_url=None, limit=None):
    matches = []
    truncated = limit is not None and len(hits) > limit
    for hit in hits[:limit] if limit is not None else hits:
        chunk = hit.chunk
        uri = None
        if public_base_url:
            uri = (public_base_url.rstrip('/') + '/app/knowledge/' + quote(hit.entry_id, safe='')
                   + '/versions/' + str(hit.version) + '#' + quote(chunk.chunk_id, safe=''))
        locator = ' / '.join(chunk.heading_path) or '正文'
        matches.append({'source_id': hit.source_id, 'title': hit.title, 'content': chunk.content,
            'source_uri': uri, 'source_locator': f'{locator} · 行 {chunk.line_start}–{chunk.line_end}',
            'updated_at': datetime.fromtimestamp(hit.updated_at / 1000, timezone.utc).isoformat()})
    return {'matches': matches, 'truncated': truncated}

"""Authenticated team knowledge management endpoints."""
import json
import re
from urllib.parse import unquote_to_bytes
from uuid import UUID
from typing import Literal

from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from workbench_agent.api.auth import authenticate
from workbench_agent.api.common import response, revision
from workbench_agent.application.errors import DomainError
from workbench_agent.knowledge.models import KBDraft
from workbench_agent.knowledge.service import KnowledgeService
from workbench_agent.knowledge.search import KnowledgeSearch
from workbench_agent.knowledge.models import KBScope
from workbench_agent.knowledge.gaps import KnowledgeGaps
from workbench_agent.knowledge.replay import KBReplayService

router = APIRouter(prefix='/v1/kb')


class SearchScope(BaseModel):
    model_config = ConfigDict(extra='forbid')
    service: str = Field(min_length=1, max_length=128, pattern=r'^[^*,\s]+$')
    environment: str = Field(min_length=1, max_length=128, pattern=r'^[^*,\s]+$')
    region: str = Field(min_length=1, max_length=128, pattern=r'^[^*,\s]+$')


class SearchBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    query: str = Field(min_length=1, max_length=4000)
    scope: SearchScope
    limit: int = Field(default=3, ge=1, le=10, strict=True)


class GapUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    status: Literal['resolved', 'dismissed']
    note: str = Field(min_length=1, max_length=2048)
    entry_id: UUID | None = None
    version: int | None = Field(default=None, ge=1, strict=True)
    verification_run_id: UUID | None = None


@router.get('/gaps')
async def list_gaps(request: Request, auth=Depends(authenticate), limit: int = Query(50, ge=1, le=100),
                    after: UUID | None = None, status: Literal['open', 'resolved', 'dismissed'] | None = 'open'):
    return await KnowledgeGaps(request.app.state.backend).list(auth, limit=limit, after=str(after) if after else None, status=status)


@router.get('/gaps/{gap_id}')
async def get_gap(gap_id: UUID, request: Request, auth=Depends(authenticate)):
    return await KnowledgeGaps(request.app.state.backend).get(auth, str(gap_id))


@router.post('/gaps/{gap_id}/draft')
async def gap_draft(gap_id: UUID, request: Request, auth=Depends(authenticate),
                    expected: int = Depends(revision), idempotency_key: str = Header()):
    return response(await KnowledgeGaps(request.app.state.backend).to_draft(auth, str(gap_id), expected_revision=expected, key=idempotency_key))


@router.post('/gaps/{gap_id}/replay')
async def replay_gap(gap_id: UUID, request: Request, auth=Depends(authenticate),
                     expected: int = Depends(revision), idempotency_key: str = Header()):
    return response(await KBReplayService(request.app.state.backend, request.app.state.runs).replay(
        auth, str(gap_id), expected_revision=expected, key=idempotency_key))


@router.patch('/gaps/{gap_id}')
async def update_gap(gap_id: UUID, body: GapUpdate, request: Request, auth=Depends(authenticate),
                     expected: int = Depends(revision), idempotency_key: str = Header()):
    return response(await KnowledgeGaps(request.app.state.backend).update(auth, str(gap_id), status=body.status,
        entry_id=str(body.entry_id) if body.entry_id else None, version=body.version,
        verification_run_id=str(body.verification_run_id) if body.verification_run_id else None,
        note=body.note, expected_revision=expected, key=idempotency_key))


@router.post('/search')
async def search(body: SearchBody, request: Request, auth=Depends(authenticate)):
    scope = KBScope(team_id=auth.team_id, **body.scope.model_dump())
    return await KnowledgeSearch(request.app.state.backend).preview(auth, scope, body.query, limit=body.limit)


def service(request):
    return KnowledgeService(request.app.state.backend)


async def bounded_body(request, maximum):
    result = bytearray()
    async for part in request.stream():
        result.extend(part)
        if len(result) > maximum:
            raise DomainError('INPUT_TOO_LARGE', 413)
    return bytes(result)


async def parse_draft(request):
    raw = await bounded_body(request, 128 * 1024)
    try:
        return KBDraft.model_validate(json.loads(raw))
    except (ValueError, UnicodeError, ValidationError):
        raise DomainError('KB_INVALID_CONTENT', 422) from None


@router.get('/entries')
async def list_entries(request: Request, auth=Depends(authenticate), limit: int = Query(50, ge=1, le=100),
                       after: UUID | None = None, q: str | None = Query(None, max_length=128),
                       kind: str | None = None, status: str | None = None, service_name: str | None = Query(None, alias='service'),
                       environment: str | None = None, region: str | None = None):
    return await service(request).list(auth, limit=limit, after=str(after) if after else None, q=q,
        kind=kind, status=status, service=service_name, environment=environment, region=region)


@router.post('/entries')
async def create_entry(request: Request, auth=Depends(authenticate), idempotency_key: str = Header()):
    return response(await service(request).create(auth, await parse_draft(request), key=idempotency_key))


@router.get('/entries/{entry_id}')
async def get_entry(entry_id: UUID, request: Request, auth=Depends(authenticate)):
    return await service(request).get(auth, str(entry_id))


@router.patch('/entries/{entry_id}')
async def save_entry(entry_id: UUID, request: Request, auth=Depends(authenticate),
                     expected: int = Depends(revision), idempotency_key: str = Header()):
    return response(await service(request).save(auth, str(entry_id), await parse_draft(request),
                                               expected_revision=expected, key=idempotency_key))


@router.post('/entries/{entry_id}/publish')
async def publish_entry(entry_id: UUID, request: Request, auth=Depends(authenticate),
                        expected: int = Depends(revision), idempotency_key: str = Header()):
    return response(await service(request).publish(auth, str(entry_id), expected_revision=expected, key=idempotency_key))


@router.post('/entries/{entry_id}/archive')
async def archive_entry(entry_id: UUID, request: Request, auth=Depends(authenticate),
                        expected: int = Depends(revision), idempotency_key: str = Header()):
    return response(await service(request).archive(auth, str(entry_id), expected_revision=expected, key=idempotency_key))


@router.get('/entries/{entry_id}/versions')
async def versions(entry_id: UUID, request: Request, auth=Depends(authenticate),
                   limit: int = Query(50, ge=1, le=100), after: int | None = Query(None, ge=1)):
    return await service(request).versions(auth, str(entry_id), limit=limit, after=after)


@router.get('/entries/{entry_id}/versions/{version}')
async def version(entry_id: UUID, version: int, request: Request, auth=Depends(authenticate)):
    return await service(request).version(auth, str(entry_id), version)


@router.post('/entries/{entry_id}/versions/{version}/rollback')
async def rollback(entry_id: UUID, version: int, request: Request, auth=Depends(authenticate),
                   expected: int = Depends(revision), idempotency_key: str = Header()):
    return response(await service(request).rollback(auth, str(entry_id), version, expected_revision=expected, key=idempotency_key))


@router.post('/entries/{entry_id}/import')
async def import_entry(entry_id: UUID, request: Request, auth=Depends(authenticate),
                       expected: int = Depends(revision), idempotency_key: str = Header(),
                       x_filename: str = Header(max_length=1024)):
    if request.headers.get('content-type', '').split(';')[0].strip().lower() not in {'text/markdown', 'text/plain'}:
        raise DomainError('KB_INVALID_CONTENT', 422)
    try:
        if re.search(r'%(?![0-9a-fA-F]{2})', x_filename):
            raise ValueError('invalid filename encoding')
        filename = unquote_to_bytes(x_filename).decode('utf-8')
        if (not filename or len(filename.encode('utf-8')) > 255 or '/' in filename or '\\' in filename
                or any(ord(char) < 32 or ord(char) == 127 for char in filename)):
            raise ValueError('invalid filename')
    except (ValueError, UnicodeError):
        raise DomainError('KB_INVALID_CONTENT', 422) from None
    raw = await bounded_body(request, 16_387)
    return response(await service(request).import_text(auth, str(entry_id), raw, filename=filename,
                                                       expected_revision=expected, key=idempotency_key))

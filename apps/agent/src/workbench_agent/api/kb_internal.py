"""Read-only service-to-service knowledge query endpoint."""

import json
from fastapi import APIRouter, Request

from workbench_agent.api.knowledge import bounded_body
from workbench_agent.application.errors import DomainError
from workbench_agent.knowledge.query_contract import knowledge_data, normalize_query
from workbench_agent.knowledge.search import KnowledgeSearch

router = APIRouter(prefix='/internal/kb')


@router.post('/query')
async def query(request: Request):
    access = request.app.state.knowledge_access
    if not access.settings.enabled:
        raise DomainError('KB_INTERNAL_UNAUTHORIZED', 401)
    access.authenticate(request.headers.get('authorization'))
    raw = await bounded_body(request, 32 * 1024)
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeError):
        raise DomainError('KB_INVALID_CONTENT', 422) from None
    scope, query_text, _scene, limit = normalize_query(body)
    authorized = access.authorize(request.headers.get('authorization'), scope)
    hits = await KnowledgeSearch(request.app.state.backend).search(
        authorized, query_text, limit=limit + 1
    )
    return knowledge_data(hits, public_base_url=access.settings.public_base_url, limit=limit)

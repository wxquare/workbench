"""Administrator-controlled tool binding drafts and immutable publication."""

from uuid import UUID
from fastapi import APIRouter, Depends, Header, Request, Query
from fastapi.responses import JSONResponse
from workbench_agent.api.auth import authenticate
from workbench_agent.api.common import revision
from workbench_agent.application.errors import DomainError
from workbench_agent.domain.tools import ToolBinding
from workbench_agent.tools.registry import ToolRegistry
from workbench_agent.tools.management import ToolManagement

router = APIRouter(prefix='/v1/agents/{agent_id}/tool-bindings')


def registry(request):
    return ToolRegistry(
        request.app.state.backend,
        mcp_connections=request.app.state.settings.service.mcp_connections,
    )


def management(request):
    return ToolManagement(
        request.app.state.backend, request.app.state.settings.service.mcp_connections
    )


@router.get('')
async def list_bindings(
    agent_id: UUID,
    request: Request,
    auth=Depends(authenticate),
    limit: int = Query(default=50, ge=1, le=100),
    after: str | None = Query(default=None, max_length=128),
):
    return await management(request).list_bindings(auth, str(agent_id), limit=limit, after=after)


@router.get('/{alias}')
async def get_binding(agent_id: UUID, alias: str, request: Request, auth=Depends(authenticate)):
    return await management(request).get_binding(auth, str(agent_id), alias)


@router.get('/{alias}/versions/{version}')
async def get_version(
    agent_id: UUID, alias: str, version: int, request: Request, auth=Depends(authenticate)
):
    return await management(request).get_version(auth, str(agent_id), alias, version)


# Declare static and version routes before the generic alias route above.
router.routes.sort(
    key=lambda route: (
        0
        if route.path.endswith('/versions/{version}')
        else 1
        if route.path == '/v1/agents/{agent_id}/tool-bindings'
        else 2
        if route.path.endswith('}') and route.path.count('{') == 2
        else 3
    )
)


@router.put('/{alias}')
async def draft(
    agent_id: UUID,
    alias: str,
    body: ToolBinding,
    request: Request,
    auth=Depends(authenticate),
    expected: int = Depends(revision),
    idempotency_key: str = Header(),
):
    if alias != body.alias:
        raise DomainError('INVALID_REQUEST', 422)
    if not body.read_only:
        raise DomainError('WRITE_BINDING_HTTP_DISABLED', 422)
    return await registry(request).save_draft(
        auth,
        str(agent_id),
        body,
        expected_revision=expected,
        key=idempotency_key,
        managed=True,
    )


@router.post('/{alias}/publish', status_code=201)
async def publish(
    agent_id: UUID,
    alias: str,
    request: Request,
    auth=Depends(authenticate),
    expected: int = Depends(revision),
    idempotency_key: str = Header(),
):
    ref = await registry(request).publish_draft(
        auth, str(agent_id), alias, expected_revision=expected, key=idempotency_key
    )
    return ref.model_dump(mode='json')


@router.post('/{alias}/probe', status_code=202)
async def probe(
    agent_id: UUID,
    alias: str,
    request: Request,
    auth=Depends(authenticate),
    idempotency_key: str = Header(),
):
    from dataclasses import asdict

    return asdict(
        await ToolRegistry(request.app.state.backend).enqueue_probe(
            auth, str(agent_id), alias, key=idempotency_key
        )
    )

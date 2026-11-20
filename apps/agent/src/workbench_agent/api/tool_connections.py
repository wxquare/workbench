from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, ConfigDict

from workbench_agent.api.auth import authenticate
from workbench_agent.tools.management import ToolManagement


router = APIRouter(prefix='/v1/agents/{agent_id}/tool-connections')


class DiscoveryBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    credential_scope: str


@router.get('')
async def connections(agent_id: UUID, request: Request, auth=Depends(authenticate)):
    return await ToolManagement(
        request.app.state.backend,
        request.app.state.settings.service.mcp_connections,
    ).connections(auth, str(agent_id))


@router.post('/{server}/discover', status_code=202)
async def discover(
    agent_id: UUID,
    server: str,
    body: DiscoveryBody,
    request: Request,
    auth=Depends(authenticate),
    idempotency_key: str = Header(),
):
    return await ToolManagement(
        request.app.state.backend,
        request.app.state.settings.service.mcp_connections,
    ).enqueue_discovery(
        auth,
        str(agent_id),
        server=server,
        credential_scope=body.credential_scope,
        key=idempotency_key,
    )

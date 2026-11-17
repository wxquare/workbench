"""Repository snapshot admission; network and Git work run in management jobs."""
from dataclasses import asdict
from uuid import UUID
from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, ConfigDict
from workbench_agent.api.auth import authenticate
from workbench_agent.execution.repositories import RepositoryService

router = APIRouter(prefix='/v1/agents/{agent_id}/repositories')

class SyncBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    commit: str

@router.post('/{repo_id}/sync', status_code=202)
async def sync(agent_id: UUID, repo_id: str, body: SyncBody, request: Request,
               auth=Depends(authenticate), idempotency_key: str=Header()):
    return asdict(await RepositoryService(request.app.state.backend).sync(auth, str(agent_id), repo_id, body.commit, key=idempotency_key))

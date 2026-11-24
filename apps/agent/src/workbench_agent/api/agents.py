from uuid import UUID
from fastapi import APIRouter, Depends, Header, Request, Query
from pydantic import BaseModel, ConfigDict, Field
from workbench_agent.domain.profiles import ProfileDraft
from workbench_agent.api.auth import authenticate
from workbench_agent.api.common import revision, response

router = APIRouter(prefix='/v1/agents')


class AgentCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=128)


@router.get('')
async def list_agents(
    request: Request,
    auth=Depends(authenticate),
    limit: int = Query(default=20, ge=1, le=100),
    after: UUID | None = None,
):
    items = await request.app.state.profiles.list_agents(auth, limit, str(after) if after else None)
    return {'items': items, 'next_cursor': items[-1]['agent_id'] if len(items) == limit else None}


@router.post('')
async def create_agent(
    body: AgentCreate, request: Request, auth=Depends(authenticate), idempotency_key: str = Header()
):
    return response(
        await request.app.state.profiles.create_agent(
            auth, body.name, key=idempotency_key, receipt=True
        )
    )


@router.get('/{agent_id}')
async def get_agent(agent_id: UUID, request: Request, auth=Depends(authenticate)):
    return await request.app.state.profiles.get_agent(auth, str(agent_id))


@router.patch('/{agent_id}')
async def save_draft(
    agent_id: UUID,
    body: ProfileDraft,
    request: Request,
    auth=Depends(authenticate),
    expected_revision: int = Depends(revision),
    idempotency_key: str = Header(),
):
    return response(
        await request.app.state.profiles.save_draft(
            auth, str(agent_id), body, expected_revision, key=idempotency_key, receipt=True
        )
    )


@router.post('/{agent_id}/profiles/publish')
async def publish(
    agent_id: UUID,
    request: Request,
    auth=Depends(authenticate),
    expected_revision: int = Depends(revision),
    idempotency_key: str = Header(),
):
    return response(
        await request.app.state.profiles.publish(
            auth, str(agent_id), expected_revision, key=idempotency_key, receipt=True
        )
    )

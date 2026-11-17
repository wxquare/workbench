from uuid import UUID
from typing import Literal
from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, ConfigDict, Field
from workbench_agent.domain.identity import Role
from workbench_agent.application.errors import not_found
from workbench_agent.api.auth import authenticate
from workbench_agent.api.common import revision, response

router = APIRouter(prefix='/v1/teams')

class TeamCreate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=128)

class MemberUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    role: Role
    status: Literal['active', 'revoked'] = 'active'

@router.get('')
async def list_teams(request: Request, auth=Depends(authenticate)):
    return {'items': await request.app.state.teams.list_teams(auth)}

@router.post('')
async def create_team(body: TeamCreate, request: Request, auth=Depends(authenticate), idempotency_key: str = Header()):
    return response(await request.app.state.teams.create_team(auth, body.name, key=idempotency_key, receipt=True))

@router.get('/{team_id}/members/{subject:path}')
async def get_member(team_id: UUID, subject: str, request: Request, auth=Depends(authenticate)):
    if str(team_id) != auth.team_id:
        raise not_found()
    return await request.app.state.teams.get_member(auth, subject)

@router.put('/{team_id}/members/{subject:path}')
async def set_member(team_id: UUID, subject: str, body: MemberUpdate, request: Request,
    auth=Depends(authenticate), expected_revision: int = Depends(revision), idempotency_key: str = Header()):
    if str(team_id) != auth.team_id:
        raise not_found()
    return response(await request.app.state.teams.set_member(auth, subject, body.role, body.status,
        expected_revision, key=idempotency_key, receipt=True))

"""Agent-scoped schedule administration."""

from typing import Literal
from uuid import UUID
from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel, ConfigDict
from workbench_agent.api.auth import authenticate
from workbench_agent.api.common import response, revision
from workbench_agent.domain.schedules import ScheduleSpec

router = APIRouter(prefix='/v1/agents/{agent_id}/schedules')


class StatusUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    status: Literal['active', 'paused']


@router.get('')
async def list_schedules(
    agent_id: UUID,
    request: Request,
    auth=Depends(authenticate),
    limit: int = Query(default=20, ge=1, le=100),
    after: UUID | None = None,
):
    return await request.app.state.schedules.list(
        auth, str(agent_id), limit, str(after) if after else None
    )


@router.post('')
async def create_schedule(
    agent_id: UUID,
    body: ScheduleSpec,
    request: Request,
    auth=Depends(authenticate),
    idempotency_key: str = Header(),
):
    return response(
        await request.app.state.schedules.create(
            auth, str(agent_id), body, key=idempotency_key, receipt=True
        )
    )


@router.patch('/{schedule_id}')
async def set_schedule_status(
    agent_id: UUID,
    schedule_id: UUID,
    body: StatusUpdate,
    request: Request,
    auth=Depends(authenticate),
    expected_revision: int = Depends(revision),
    idempotency_key: str = Header(),
):
    return response(
        await request.app.state.schedules.set_status(
            auth,
            str(agent_id),
            str(schedule_id),
            body.status,
            expected_revision,
            key=idempotency_key,
            receipt=True,
        )
    )


@router.post('/{schedule_id}/trigger')
async def trigger_schedule(
    agent_id: UUID,
    schedule_id: UUID,
    request: Request,
    auth=Depends(authenticate),
    idempotency_key: str = Header(),
):
    return response(
        await request.app.state.schedules.trigger(
            auth, str(agent_id), str(schedule_id), key=idempotency_key, receipt=True
        )
    )


@router.get('/{schedule_id}/fires')
async def schedule_fires(
    agent_id: UUID,
    schedule_id: UUID,
    request: Request,
    auth=Depends(authenticate),
    limit: int = Query(default=100, ge=1, le=100),
    after: str | None = Query(default=None, max_length=1024),
):
    return await request.app.state.schedules.fires(
        auth, str(agent_id), str(schedule_id), limit, after
    )

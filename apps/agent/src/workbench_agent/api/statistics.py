from fastapi import APIRouter, Depends, Query, Request
from uuid import UUID

from workbench_agent.api.auth import authenticate
from workbench_agent.statistics.service import StatisticsService


router = APIRouter(prefix='/v1/stats')


@router.get('/summary')
async def summary(
    request: Request,
    start_at: str = Query(),
    end_at: str = Query(),
    agent_id: UUID | None = None,
    auth=Depends(authenticate),
):
    return await StatisticsService(request.app.state.backend).summary(
        auth,
        start_at=start_at,
        end_at=end_at,
        agent_id=str(agent_id) if agent_id else None,
    )

from uuid import UUID
from fastapi import APIRouter, Depends, Query, Request
from workbench_agent.api.auth import authenticate
from workbench_agent.incidents.service import IncidentService

router = APIRouter(prefix='/v1/incidents')


@router.get('')
async def list_incidents(
    request: Request,
    agent_id: UUID,
    limit: int = Query(default=20, ge=1, le=100),
    after: UUID | None = None,
    auth=Depends(authenticate),
):
    return await IncidentService(request.app.state.backend, request.app.state.settings).list(
        auth, str(agent_id), limit, str(after) if after else None
    )


@router.get('/{incident_id}')
async def get_incident(incident_id: UUID, request: Request, auth=Depends(authenticate)):
    return await IncidentService(request.app.state.backend, request.app.state.settings).get(
        auth, str(incident_id)
    )

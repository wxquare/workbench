from uuid import UUID
from fastapi import APIRouter, Depends, Request
from workbench_agent.api.auth import authenticate

router = APIRouter(prefix='/v1/runs/{run_id}/evidence')


@router.get('/{evidence_id}')
async def get(run_id: UUID, evidence_id: UUID, request: Request, auth=Depends(authenticate)):
    async with request.app.state.backend.create_uow().begin_read() as uow:
        return await request.app.state.contexts.evidence_content(
            uow, auth, str(run_id), str(evidence_id)
        )

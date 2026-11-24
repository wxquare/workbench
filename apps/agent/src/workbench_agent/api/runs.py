from uuid import UUID
from typing import Literal
from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import Response
from workbench_agent.application.idempotency import canonical
from workbench_agent.domain.runs import CreateRun
from workbench_agent.api.auth import authenticate

router = APIRouter(prefix='/v1/runs')


@router.get('')
async def list_runs(
    agent_id: UUID,
    request: Request,
    auth=Depends(authenticate),
    limit: int = Query(default=20, ge=1, le=100),
    after: UUID | None = None,
    status: Literal[
        'queued',
        'running',
        'finalizing',
        'cancelling',
        'waiting_input',
        'waiting_approval',
        'succeeded',
        'partial',
        'failed',
        'cancelled',
        'timed_out',
        'expired',
    ]
    | None = None,
    q: str | None = Query(default=None, max_length=200),
):
    from workbench_agent.application.inventory import InventoryService

    return await InventoryService(request.app.state.backend).runs(
        auth,
        str(agent_id),
        limit=limit,
        after=str(after) if after else None,
        status=status,
        query=q.strip() if q else None,
    )


@router.post('')
async def create_run(
    body: CreateRun, request: Request, auth=Depends(authenticate), idempotency_key: str = Header()
):
    result = await request.app.state.runs.create(auth, body, idempotency_key)
    return Response(
        canonical(result.model_dump(mode='json')),
        media_type='application/json',
        status_code=202,
        headers={'Idempotency-Replayed': 'true'} if result.replayed else {},
    )


@router.get('/{run_id}')
async def get_run(run_id: UUID, request: Request, auth=Depends(authenticate)):
    return (await request.app.state.runs.get(auth, str(run_id))).model_dump(mode='json')


from workbench_agent.domain.runs import ResumeRun


@router.post('/{run_id}/resume')
async def resume_run(
    run_id: UUID,
    body: ResumeRun,
    request: Request,
    auth=Depends(authenticate),
    idempotency_key: str = Header(),
):
    result = await request.app.state.runs.resume(auth, str(run_id), body, idempotency_key)
    return Response(
        canonical(result.model_dump(mode='json')),
        media_type='application/json',
        status_code=202,
        headers={'Idempotency-Replayed': 'true'} if result.replayed else {},
    )


@router.post('/{run_id}/cancel')
async def cancel_run(
    run_id: UUID, request: Request, auth=Depends(authenticate), idempotency_key: str = Header()
):
    return (await request.app.state.runs.cancel(auth, str(run_id), idempotency_key)).model_dump(
        mode='json'
    )

"""Read and explicitly authorize already proposed actions."""

from typing import Literal
from uuid import UUID
from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, ConfigDict, Field
from workbench_agent.api.auth import authenticate
from workbench_agent.api.common import response

router = APIRouter(prefix='/v1')


class Decision(BaseModel):
    model_config = ConfigDict(extra='forbid')
    decision: Literal['approve', 'reject']
    expected_args_hash: str = Field(pattern='^[0-9a-f]{64}$')


@router.get('/actions/{action_id}')
async def get_action(action_id: UUID, request: Request, auth=Depends(authenticate)):
    return await request.app.state.actions.get(auth, str(action_id))


@router.post('/approvals/{approval_id}/decision')
async def decide(approval_id: UUID, body: Decision, request: Request, auth=Depends(authenticate)):
    return await request.app.state.actions.authorize(
        auth, str(approval_id), body.decision, body.expected_args_hash
    )


@router.post('/actions/{action_id}/retry')
async def retry_action(
    action_id: UUID, request: Request, auth=Depends(authenticate), idempotency_key: str = Header()
):
    return response(
        await request.app.state.actions.retry(
            auth, str(action_id), key=idempotency_key, receipt=True
        )
    )


class ReapprovalRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_args_hash: str = Field(pattern='^[0-9a-f]{64}$')


@router.post('/actions/{action_id}/reapproval')
async def request_reapproval(
    action_id: UUID,
    body: ReapprovalRequest,
    request: Request,
    auth=Depends(authenticate),
    idempotency_key: str = Header(),
):
    return response(
        await request.app.state.actions.reapprove(
            auth, str(action_id), body.expected_args_hash, key=idempotency_key, receipt=True
        )
    )


@router.post('/actions/{action_id}/reconcile')
async def reconcile_action(
    action_id: UUID,
    request: Request,
    auth=Depends(authenticate),
    idempotency_key: str = Header(alias='Idempotency-Key'),
):
    from workbench_agent.application.reconciliation import ReconciliationService

    return response(
        await _reconcile(
            ReconciliationService(request.app.state.backend),
            auth,
            str(action_id),
            key=idempotency_key,
        )
    )


async def _reconcile(service, auth, action_id, *, key):
    result = await service.reconcile(auth, action_id, key=key)
    from workbench_agent.application.idempotency import MutationResult

    return MutationResult(result, 202)

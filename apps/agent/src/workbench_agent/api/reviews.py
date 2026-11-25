"""Authorized human review inbox."""

from typing import Literal

from fastapi import APIRouter, Depends, Query, Request

from workbench_agent.api.auth import authenticate
from workbench_agent.application.reviews import ReviewService


router = APIRouter(prefix='/v1/reviews')


@router.get('/summary')
async def summary(request: Request, auth=Depends(authenticate)):
    return await ReviewService(request.app.state.backend).summary(auth)


@router.get('')
async def list_reviews(
    request: Request,
    type: Literal[
        'run_input',
        'run_approval',
        'action_approval',
        'action_reapproval',
        'action_reconcile',
    ]
    | None = None,
    state: Literal['pending', 'expired'] = 'pending',
    limit: int = Query(default=20, ge=1, le=100),
    after: str | None = None,
    auth=Depends(authenticate),
):
    return await ReviewService(request.app.state.backend).list(
        auth, state=state, kind=type, limit=limit, after=after
    )


@router.get('/{item_id}')
async def get_review(item_id: str, request: Request, auth=Depends(authenticate)):
    return await ReviewService(request.app.state.backend).get(auth, item_id)

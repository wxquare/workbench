"""Authorized run feedback endpoint."""
from uuid import UUID
from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from workbench_agent.api.auth import authenticate
from workbench_agent.api.common import response
from workbench_agent.evaluation.feedback import FeedbackService

router = APIRouter(prefix='/v1/runs')


class FeedbackBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    rating: int = Field(strict=True)
    comment: str | None = Field(default=None, max_length=4000)
    reason: str | None = Field(default=None, pattern=r'^(irrelevant|citation_error|stale|other)$')

    @field_validator('rating')
    @classmethod
    def validate_rating(cls, value):
        if value not in (-1, 1):
            raise ValueError('rating must be -1 or 1')
        return value

    @model_validator(mode='after')
    def reason_matches_rating(self):
        if self.rating == 1 and self.reason is not None:
            raise ValueError('positive feedback cannot carry a reason')
        return self


@router.post('/{run_id}/feedback', status_code=201)
async def submit_feedback(run_id: UUID, body: FeedbackBody, request: Request,
                          auth=Depends(authenticate), idempotency_key: str = Header()):
    return response(await FeedbackService(request.app.state.backend).record(
        auth, str(run_id), body.rating, body.comment, idempotency_key,
        reason=body.reason))


@router.get('/{run_id}/feedback')
async def list_feedback(run_id: UUID, request: Request, auth=Depends(authenticate),
                        limit: int = Query(default=20, ge=1, le=100), after: UUID | None = None):
    return await FeedbackService(request.app.state.backend).list(
        auth, str(run_id), limit, str(after) if after else None)

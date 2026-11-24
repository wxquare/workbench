"""Team-wide Skill library endpoints."""

from uuid import UUID
from fastapi import APIRouter, Depends, Header, Query, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from workbench_agent.api.auth import authenticate
from workbench_agent.api.common import response, revision
from workbench_agent.application.errors import DomainError
from workbench_agent.skill_management.library import SharedSkillLibrary

router = APIRouter(prefix='/v1/skills')


class SkillDraft(BaseModel):
    model_config = ConfigDict(extra='forbid')
    description: str = Field(min_length=1, max_length=1000)
    content: str | None = Field(default=None, min_length=1, max_length=32000)
    files: dict[str, str | dict[str, str]] | None = Field(default=None, max_length=64)

    @model_validator(mode='after')
    def require_resources(self):
        if self.content is None and self.files is None:
            raise ValueError('content or files is required')
        return self


class SkillCreate(SkillDraft):
    name: str = Field(min_length=1, max_length=128)


def library(request):
    root = request.app.state.settings.storage.skill_object_root

    def objects():
        if root is None:
            return None
        from workbench_agent.skill_management.objects import LocalSkillObjects

        try:
            return LocalSkillObjects(root)
        except (OSError, ValueError):
            raise DomainError('OBJECT_STORAGE_UNAVAILABLE', 503) from None

    return SharedSkillLibrary(request.app.state.backend, objects)


@router.get('')
async def list_skills(
    request: Request,
    auth=Depends(authenticate),
    limit: int = Query(default=100, ge=1, le=100),
    after: UUID | None = None,
    q: str | None = Query(default=None, max_length=128),
):
    return await library(request).list(
        auth, limit, str(after) if after else None, q.strip() if q else None
    )


@router.post('')
async def create_skill(
    body: SkillCreate, request: Request, auth=Depends(authenticate), idempotency_key: str = Header()
):
    return response(
        await library(request).create(
            auth, body.name, body.description, body.content, idempotency_key, files=body.files
        )
    )


@router.get('/{skill_id}')
async def get_skill(skill_id: UUID, request: Request, auth=Depends(authenticate)):
    return await library(request).get(auth, str(skill_id))


@router.patch('/{skill_id}')
async def save_skill(
    skill_id: UUID,
    body: SkillDraft,
    request: Request,
    auth=Depends(authenticate),
    expected: int = Depends(revision),
    idempotency_key: str = Header(),
):
    return response(
        await library(request).save(
            auth,
            str(skill_id),
            body.description,
            body.content,
            expected,
            idempotency_key,
            files=body.files,
        )
    )


@router.post('/{skill_id}/publish')
async def publish_skill(
    skill_id: UUID,
    request: Request,
    auth=Depends(authenticate),
    expected: int = Depends(revision),
    idempotency_key: str = Header(),
):
    return response(await library(request).publish(auth, str(skill_id), expected, idempotency_key))


@router.get('/{skill_id}/versions')
async def versions(skill_id: UUID, request: Request, auth=Depends(authenticate)):
    detail = await library(request).get(auth, str(skill_id))
    return {'items': detail['versions'], 'next_cursor': None}


@router.get('/{skill_id}/versions/{version}')
async def version(skill_id: UUID, version: int, request: Request, auth=Depends(authenticate)):
    return await library(request).version(auth, str(skill_id), version)

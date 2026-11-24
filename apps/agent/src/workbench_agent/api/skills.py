"""Skill import admission and immutable publication."""

from dataclasses import asdict
from uuid import UUID
from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, ConfigDict
from workbench_agent.api.auth import authenticate
from workbench_agent.api.common import revision
from workbench_agent.application.errors import DomainError
from workbench_agent.skill_management.importer import SkillImporter
from workbench_agent.skill_management.versions import SkillService

router = APIRouter(prefix='/v1/agents/{agent_id}/skills')


class GitBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    repository: str
    commit: str


class ArtifactBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    artifact_id: UUID


class PublishBody(BaseModel):
    model_config = ConfigDict(extra='forbid')
    job_id: UUID


@router.post('/{name}/import', status_code=202)
async def import_seed(
    agent_id: UUID,
    name: str,
    request: Request,
    auth=Depends(authenticate),
    idempotency_key: str = Header(),
):
    return asdict(
        await SkillImporter(request.app.state.backend).import_seed(
            auth, str(agent_id), name, key=idempotency_key
        )
    )


@router.post('/{name}/import/git', status_code=202)
async def import_git(
    agent_id: UUID,
    name: str,
    body: GitBody,
    request: Request,
    auth=Depends(authenticate),
    idempotency_key: str = Header(),
):
    return asdict(
        await SkillImporter(request.app.state.backend).import_git(
            auth, str(agent_id), name, body.repository, body.commit, key=idempotency_key
        )
    )


@router.post('/{name}/import/artifact', status_code=202)
async def import_artifact(
    agent_id: UUID,
    name: str,
    body: ArtifactBody,
    request: Request,
    auth=Depends(authenticate),
    idempotency_key: str = Header(),
):
    return asdict(
        await SkillImporter(request.app.state.backend).import_artifact(
            auth, str(agent_id), name, str(body.artifact_id), key=idempotency_key
        )
    )


@router.post('/{name}/publish', status_code=201)
async def publish(
    agent_id: UUID,
    name: str,
    body: PublishBody,
    request: Request,
    auth=Depends(authenticate),
    expected: int = Depends(revision),
    idempotency_key: str = Header(),
):
    root = request.app.state.settings.storage.skill_object_root
    if root is None:
        raise DomainError('OBJECT_STORAGE_UNAVAILABLE', 503)
    from workbench_agent.skill_management.objects import LocalSkillObjects

    service = SkillService(request.app.state.backend, LocalSkillObjects(root))
    ref = await service.publish(
        auth,
        str(agent_id),
        str(body.job_id),
        expected_revision=expected,
        key=idempotency_key,
        expected_name=name,
    )
    return ref.model_dump(mode='json')

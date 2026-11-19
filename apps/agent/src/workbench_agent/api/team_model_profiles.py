"""Team model library; Agent bindings use its published versions."""
from dataclasses import asdict
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse

from workbench_agent.api.auth import authenticate
from workbench_agent.api.common import response, revision
from workbench_agent.api.model_profiles import CredentialInput
from workbench_agent.application.model_profiles import ModelProfileService
from workbench_agent.runtime.models.factory import ModelCredentialPolicy
from workbench_agent.settings import ModelProfile

router = APIRouter(prefix='/v1/model-profiles')


def service(request: Request):
    return ModelProfileService(request.app.state.backend)


@router.get('')
async def list_profiles(request: Request, auth=Depends(authenticate),
                        limit: int = Query(default=20, ge=1, le=100), after: UUID | None = None):
    return await service(request).list(auth, None, limit, str(after) if after else None)


@router.post('')
async def create(body: ModelProfile, request: Request, auth=Depends(authenticate), idempotency_key: str = Header()):
    return response(await service(request).create(auth, None, body, idempotency_key))


@router.get('/{name}')
async def get(name: str, request: Request, auth=Depends(authenticate)):
    return await service(request).get(auth, None, name)


@router.patch('/{name}')
async def patch(name: str, body: dict, request: Request, auth=Depends(authenticate),
                expected: int = Depends(revision), idempotency_key: str = Header()):
    return response(await service(request).patch(auth, None, name, body, expected, idempotency_key))


@router.put('/{name}/credential')
async def set_credential(name: str, body: CredentialInput, request: Request, auth=Depends(authenticate),
                         expected: int = Depends(revision)):
    result = await service(request).set_credential(auth, None, name, body.api_key.get_secret_value(), expected,
        request.app.state.managed_model_secrets,
        ModelCredentialPolicy(request.app.state.settings.service.model_credential_bindings))
    return JSONResponse(result, headers={'Cache-Control': 'no-store'})


@router.post('/{name}/publish')
async def publish(name: str, request: Request, auth=Depends(authenticate),
                  expected: int = Depends(revision), idempotency_key: str = Header()):
    return response(await service(request).publish(auth, None, name, expected, idempotency_key))


@router.post('/{name}/probe', status_code=202)
async def probe(name: str, request: Request, auth=Depends(authenticate), idempotency_key: str = Header()):
    policy = ModelCredentialPolicy(request.app.state.settings.service.model_credential_bindings)
    return asdict(await service(request).probe(auth, None, name, idempotency_key, policy))

"""Bounded upload admission; external storage runs in a durable worker job."""
from dataclasses import asdict
from hashlib import sha256
from uuid import UUID
from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, ConfigDict, Field
from workbench_agent.api.auth import authenticate
from workbench_agent.application.artifacts import ArtifactService
from workbench_agent.application.errors import DomainError
from workbench_agent.execution.uploads import ArtifactUploadQueue, UploadStaging, MAX_BYTES

router=APIRouter(prefix='/v1')

class UploadBody(BaseModel):
    model_config=ConfigDict(extra='forbid')
    kind: str
    sha256: str
    size: int = Field(ge=0, le=MAX_BYTES, strict=True)

@router.post('/runs/{run_id}/uploads',status_code=201)
async def begin_upload(run_id:UUID,body:UploadBody,request:Request,auth=Depends(authenticate)):
    artifact=await ArtifactService(request.app.state.backend).begin_upload(auth,str(run_id),body.kind,body.sha256,body.size)
    return {key:artifact[key] for key in ('artifact_id','status','sha256','size')}

@router.post('/uploads/{artifact_id}/complete',status_code=202)
async def complete_upload(artifact_id:UUID,request:Request,auth=Depends(authenticate),idempotency_key:str=Header(),upload_replay:bool=Header(False)):
    root=request.app.state.settings.storage.upload_staging_root
    backend=request.app.state.backend
    queue=ArtifactUploadQueue(backend,None)
    prior,size,expected=await queue.replay_info(auth,str(artifact_id),idempotency_key)
    if upload_replay:
        async for chunk in request.stream():
            if chunk: raise DomainError('UPLOAD_REPLAY_BODY_FORBIDDEN',422)
        if prior is None: raise DomainError('UPLOAD_REPLAY_NOT_FOUND',409)
        return asdict(prior)
    if prior is not None:
        # Verify the original fixed body without buffering or touching staging.
        length=0;checksum=sha256()
        async for chunk in request.stream():
            length+=len(chunk)
            if length>size: raise DomainError('IDEMPOTENCY_CONFLICT',409)
            checksum.update(chunk)
        if length!=size or checksum.hexdigest()!=expected: raise DomainError('IDEMPOTENCY_CONFLICT',409)
        return asdict(prior)
    if root is None: raise DomainError('UPLOAD_STAGING_UNAVAILABLE',503)
    async with backend.create_uow().begin_write() as uow:
        artifact=await ArtifactService(backend)._upload(uow,auth,str(artifact_id))
    data=bytearray()
    async for chunk in request.stream():
        if len(data)+len(chunk)>artifact['size'] or len(data)+len(chunk)>MAX_BYTES: raise DomainError('UPLOAD_TOO_LARGE',413)
        data.extend(chunk)
    return asdict(await ArtifactUploadQueue(backend,UploadStaging(root)).submit(auth,str(artifact_id),bytes(data),key=idempotency_key))

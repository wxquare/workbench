from dataclasses import asdict
from uuid import UUID
from fastapi import APIRouter,Depends,Header,Request,Query
from fastapi.responses import JSONResponse
from pydantic import Field, SecretStr
from workbench_agent.api.auth import authenticate
from workbench_agent.api.common import response,revision
from workbench_agent.application.model_profiles import ModelProfileService
from workbench_agent.settings import ConfigModel, ModelProfile

router=APIRouter(prefix='/v1/agents/{agent_id}/model-profiles')

class CredentialInput(ConfigModel):
    api_key: SecretStr = Field(min_length=1, max_length=4096)

@router.get('')
async def list_profiles(agent_id:UUID,request:Request,auth=Depends(authenticate),
    limit:int=Query(default=20,ge=1,le=100),after:UUID|None=None):
    return await ModelProfileService(request.app.state.backend).list(auth,str(agent_id),limit,str(after) if after else None)

@router.post('')
async def create(agent_id:UUID,body:ModelProfile,request:Request,auth=Depends(authenticate),idempotency_key:str=Header()):
    return response(await ModelProfileService(request.app.state.backend).create(auth,str(agent_id),body,idempotency_key))

@router.get('/{name}')
async def get(agent_id:UUID,name:str,request:Request,auth=Depends(authenticate)):
    return await ModelProfileService(request.app.state.backend).get(auth,str(agent_id),name)

@router.patch('/{name}')
async def patch(agent_id:UUID,name:str,body:dict,request:Request,auth=Depends(authenticate),expected:int=Depends(revision),idempotency_key:str=Header()):
    return response(await ModelProfileService(request.app.state.backend).patch(auth,str(agent_id),name,body,expected,idempotency_key))

@router.put('/{name}/credential')
async def set_credential(agent_id:UUID,name:str,body:CredentialInput,request:Request,
    auth=Depends(authenticate),expected:int=Depends(revision)):
    from workbench_agent.runtime.models.factory import ModelCredentialPolicy
    result=await ModelProfileService(request.app.state.backend).set_credential(
        auth,str(agent_id),name,body.api_key.get_secret_value(),expected,request.app.state.managed_model_secrets,
        ModelCredentialPolicy(request.app.state.settings.service.model_credential_bindings))
    return JSONResponse(result,headers={'Cache-Control':'no-store'})

@router.post('/{name}/publish')
async def publish(agent_id:UUID,name:str,request:Request,auth=Depends(authenticate),expected:int=Depends(revision),idempotency_key:str=Header()):
    result=await ModelProfileService(request.app.state.backend).publish(auth,str(agent_id),name,expected,idempotency_key)
    return response(result)

@router.post('/{name}/probe',status_code=202)
async def probe(agent_id:UUID,name:str,request:Request,auth=Depends(authenticate),idempotency_key:str=Header()):
    return asdict(await ModelProfileService(request.app.state.backend).probe(auth,str(agent_id),name,idempotency_key))

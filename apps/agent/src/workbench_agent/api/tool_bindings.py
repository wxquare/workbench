"""Administrator-controlled tool binding drafts and immutable publication."""
from uuid import UUID
from fastapi import APIRouter,Depends,Header,Request
from fastapi.responses import JSONResponse
from workbench_agent.api.auth import authenticate
from workbench_agent.api.common import revision
from workbench_agent.application.errors import DomainError
from workbench_agent.domain.tools import ToolBinding
from workbench_agent.tools.registry import ToolRegistry

router=APIRouter(prefix='/v1/agents/{agent_id}/tool-bindings')

@router.put('/{alias}')
async def draft(agent_id:UUID,alias:str,body:ToolBinding,request:Request,auth=Depends(authenticate),expected:int=Depends(revision),idempotency_key:str=Header()):
    if alias!=body.alias: raise DomainError('INVALID_REQUEST',422)
    return await ToolRegistry(request.app.state.backend).save_draft(auth,str(agent_id),body,expected_revision=expected,key=idempotency_key)

@router.post('/{alias}/publish',status_code=201)
async def publish(agent_id:UUID,alias:str,request:Request,auth=Depends(authenticate),expected:int=Depends(revision),idempotency_key:str=Header()):
    ref=await ToolRegistry(request.app.state.backend).publish_draft(auth,str(agent_id),alias,expected_revision=expected,key=idempotency_key)
    return ref.model_dump(mode='json')

@router.post('/{alias}/probe',status_code=202)
async def probe(agent_id:UUID,alias:str,request:Request,auth=Depends(authenticate),idempotency_key:str=Header()):
    from dataclasses import asdict
    return asdict(await ToolRegistry(request.app.state.backend).enqueue_probe(auth,str(agent_id),alias,key=idempotency_key))

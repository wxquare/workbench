from fastapi import APIRouter,Request,Depends
from workbench_agent.api.auth import authenticate
router=APIRouter()

@router.get('/v1/artifacts/{artifact_id}/url')
async def artifact_url(artifact_id:str,request:Request,auth=Depends(authenticate)):
    return await request.app.state.artifacts.url(auth,artifact_id)

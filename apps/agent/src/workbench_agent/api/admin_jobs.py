from dataclasses import asdict
from uuid import UUID
from fastapi import APIRouter,Depends,Request
from workbench_agent.api.auth import authenticate
from workbench_agent.application.authorization import Authorization
from workbench_agent.domain.admin_jobs import AdminAuth
from workbench_agent.jobs.admin import AdminJobService
from workbench_agent.application.errors import not_found

router=APIRouter(prefix='/v1/agents/{agent_id}/admin-jobs')

@router.get('/{job_id}')
async def get(agent_id:UUID,job_id:UUID,request:Request,auth=Depends(authenticate)):
    backend=request.app.state.backend
    async with backend.create_uow().begin_read() as uow:
        await Authorization(backend).current(uow,auth,str(agent_id),'admin')
    principal=AdminAuth(auth.team_id,str(agent_id),auth.subject,True)
    try: job=await AdminJobService(backend).get(principal,str(job_id))
    except LookupError: raise not_found() from None
    return asdict(job)

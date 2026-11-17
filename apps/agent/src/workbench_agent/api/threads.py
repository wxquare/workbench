from uuid import UUID
from fastapi import APIRouter, Depends, Header, Request, Query
from workbench_agent.domain.runs import CreateThread
from workbench_agent.api.auth import authenticate
from workbench_agent.api.common import response

router = APIRouter(prefix='/v1/threads')

@router.get('')
async def list_threads(agent_id: UUID, request: Request, auth=Depends(authenticate),
    limit: int = Query(default=20, ge=1, le=100), after: UUID | None = None):
    from workbench_agent.application.inventory import InventoryService
    return await InventoryService(request.app.state.backend).threads(auth, str(agent_id), limit=limit,
        after=str(after) if after else None)

@router.post('')
async def create_thread(body: CreateThread, request: Request, auth=Depends(authenticate), idempotency_key: str = Header()):
    return response(await request.app.state.runs.create_thread(auth, body, idempotency_key))

@router.get('/{thread_id}')
async def get_thread(thread_id: UUID, request: Request, auth=Depends(authenticate)):
    return (await request.app.state.runs.get_thread(auth, str(thread_id))).model_dump(mode='json')

@router.get('/{thread_id}/messages')
async def messages(thread_id: UUID, request: Request, auth=Depends(authenticate),
    limit: int = Query(default=20, ge=1, le=100), after: UUID | None = None):
    return await request.app.state.runs.messages(auth, str(thread_id), limit=limit, after=str(after) if after else None)

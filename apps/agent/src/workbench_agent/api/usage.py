"""Usage summary for a currently authorized run."""

from uuid import UUID
from fastapi import APIRouter, Depends, Request
from workbench_agent.api.auth import authenticate
from workbench_agent.runtime.budgets.service import BudgetService

router = APIRouter(prefix='/v1')


@router.get('/usage')
async def usage(request: Request, run_id: UUID, auth=Depends(authenticate)):
    result = await BudgetService(request.app.state.backend).snapshot(auth, str(run_id))
    return result.model_dump(mode='json')

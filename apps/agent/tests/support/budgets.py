"""Real caller-UoW budget operations shared by integration scenarios."""
from workbench_agent.domain.usage import ReservationRequest,Reservation
from workbench_agent.runtime.budgets.service import BudgetService

async def reserve_usage(platform,guard,*,invocation_id,kind='expert',model_attempts=None,
        tool_attempts=None,token_upper_bound=None):
    tool=kind=='tool'
    request=ReservationRequest(invocation_id=invocation_id,kind=kind,
        model_attempts=(0 if tool else 1) if model_attempts is None else model_attempts,
        tool_attempts=(1 if tool else 0) if tool_attempts is None else tool_attempts,
        token_upper_bound=(0 if tool else 100) if token_upper_bound is None else token_upper_bound)
    async with platform.backend.create_uow().begin_write() as uow:
        return await platform.budgets.reserve(guard,request,uow=uow)

def is_reserved(result):
    return isinstance(result,Reservation) and result.status=='reserved'

"""Public authorized cancellation, serialized with resume and finalization."""
from workbench_agent.application.errors import not_found
from workbench_agent.domain.runs import RunView
class CancelService:
    def __init__(self,runs): self.runs=runs;self.backend=runs.backend
    async def cancel(self,auth,run_id,key):
        async with self.backend.create_uow().begin_write() as uow:
            run=await self.backend.runs.one(uow,'runs',team_id=auth.team_id,run_id=run_id)
            if run is None: raise not_found()
            await self.runs.authorization.current(uow,auth,run['agent_id'],'operator')
            async def operation():
                await self.backend.controls.request_cancel(uow,run)
                # Save the byte-original public response, not a later status read.
                return (await self.runs.view(uow,auth,run_id)).model_dump(mode='json')
            result=await self.runs.idempotency.execute(uow,auth,run['agent_id'],'run.cancel:'+run_id,key,{},operation,status=200)
        # RunContext is excluded from public JSON and remains pinned internally.
        from workbench_agent.domain.context import RunContext
        return RunView(**result.body,context=RunContext.model_validate_json(run['versions']))

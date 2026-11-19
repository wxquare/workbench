"""Versioned conversion of recorded checkpoints; never changes pinned graphs."""
from workbench_agent.runtime.graphs.ids import GraphId
from workbench_agent.domain.outcomes import RunOutcome
from workbench_agent.application.results import ResultService


def saved_outcome(saved, graph_id):
    """Return only a durable completed graph outcome."""
    if not saved or saved['interrupts']:
        return None
    outcome = saved['values'].get('outcome')
    return RunOutcome.model_validate(outcome) if outcome is not None else None


class ResultProjector:
    def __init__(self,backend,waiting_projector): self.backend,self.waiting_projector=backend,waiting_projector
    async def project(self,uow,claim,result):
        saved=await self.backend.runs.checkpoint(uow,claim.guard.team_id,claim.guard.run_id)
        outcome=saved_outcome(saved,claim.context.graph_id)
        if outcome is None: return await self.waiting_projector.project(uow,claim,result)
        from workbench_agent.runtime.graphs.evidence_authority import sanitize_v8_outcome
        if claim.context.graph_id not in (GraphId.RCA_SYNTHETIC,GraphId.RCA_PROPOSAL,GraphId.RCA_BATCH_PROPOSAL):
            outcome = await sanitize_v8_outcome(self.backend,uow,claim,outcome)
        await ResultService(self.backend).finalize_in(uow,claim.guard,outcome,outcome.digest())

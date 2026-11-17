"""Versioned conversion of recorded checkpoints; never changes pinned graphs."""
from typing import Literal
from pydantic import Field
from workbench_agent.domain.profiles import FrozenModel
from workbench_agent.domain.outcomes import RunOutcome,UsageSummary
from workbench_agent.application.results import ResultService

class RecordedFoundation(FrozenModel):
    kind:Literal['foundation.recorded.v1']
    completed:Literal[True]
    summary:Literal['Foundation input recorded.']
    input_text:str=Field(min_length=1)
    input_values:dict[str,str]
    approval_decision:Literal['approve','reject']|None
    last_applied_resume_id:str|None

def saved_outcome(saved,graph_version):
    """T09 uses this before deciding replay/exhaustion; None means no final evidence."""
    if not saved or saved['interrupts']: return None
    values=saved['values']
    if values.get('outcome') is not None:
        return RunOutcome.model_validate(values['outcome'])
    if graph_version in ('dod.v2','dod.v3') and values.get('final_progress') is not None:
        RecordedFoundation.model_validate(values['final_progress'])
    elif graph_version=='dod.v1' and values.get('progress')=='composed':
        # v1 had no descriptor. Its stable recorded AI message proves composition.
        from langchain_core.messages import AIMessage
        messages=values.get('messages',[])
        if not any(type(m) is AIMessage and m.id==values['run_id']+':foundation' and m.content=='Foundation input recorded.' for m in messages):
            raise ValueError('legacy foundation composition evidence missing')
    else: return None
    summary='Foundation input recorded. No business diagnosis or model inference was performed.'
    if graph_version=='dod.v3':
        decision=values.get('final_progress',{}).get('approval_decision')
        if decision is not None:
            explanation=('approved.' if decision=='approve' else 'rejected; confirmation step skipped.')
            summary=('Foundation recording confirmation '+explanation+
                ' Submitted input and the decision remain recorded. No external action, business diagnosis or model inference was performed.')
    return RunOutcome(kind='foundation.recorded.v1',completed=True,
        summary=summary,
        findings=(),root_cause_hypotheses=(),impact=None,evidence_refs=(),unresolved_items=(),
        proposed_actions=(),knowledge_gaps=(),usage_summary=UsageSummary(status='complete',input_tokens=0,output_tokens=0,amount_micros=0,currency='USD'))

class ResultProjector:
    def __init__(self,backend,waiting_projector): self.backend,self.waiting_projector=backend,waiting_projector
    async def project(self,uow,claim,result):
        saved=await self.backend.runs.checkpoint(uow,claim.guard.team_id,claim.guard.run_id)
        outcome=saved_outcome(saved,claim.context.graph_version)
        if outcome is None: return await self.waiting_projector.project(uow,claim,result)
        from workbench_agent.runtime.graphs.evidence_authority import sanitize_v8_outcome
        if claim.context.graph_version not in ('dod.v13','dod.v14','dod.v15'):
            outcome = await sanitize_v8_outcome(self.backend,uow,claim,outcome)
        await ResultService(self.backend).finalize_in(uow,claim.guard,outcome,outcome.digest())

"""dod.v3 public recording inputs. No business inference or external action policy."""
from workbench_agent.application.idempotency import digest
from workbench_agent.application.errors import DomainError


def recording_fields(input_value, context):
    """Map an unresolved region choice or recording confirmation to one named wait.

    Other business_context keys remain opaque, but bind the confirmation digest.
    This mapping applies only to new dod.v3 admissions and initial checkpoints.
    """
    candidates=context.get('region_candidates',[])
    region=context.get('region')
    confirm=context.get('confirm_recording',False)
    if (not isinstance(candidates,list) or
        any(not isinstance(value,str) or not value.strip() for value in candidates) or
        ('region' in context and (not isinstance(region,str) or not region.strip())) or
        not isinstance(confirm,bool)):
        raise DomainError('INVALID_FOUNDATION_CONTEXT',422)
    distinct=set(candidates)
    conflict=bool(distinct) and (region not in distinct if region is not None else len(distinct)>1)
    if conflict and confirm:
        raise DomainError('INVALID_FOUNDATION_CONTEXT',422)
    selected=region if region is not None else next(iter(distinct)) if len(distinct)==1 else None
    return {'requires_input':conflict,'requires_approval':confirm,
        'input_values':{'region':selected} if selected is not None and not conflict else {},
        'args_hash':digest({'contract':'foundation-recording-v1','input':input_value,'business_context':context})}

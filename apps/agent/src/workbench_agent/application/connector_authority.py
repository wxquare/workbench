"""Revalidate the exact connector identity bound to a synthetic Run."""
from sqlalchemy import select
from workbench_agent.application.errors import DomainError
from workbench_agent.jobs.worker import CapabilityBlocked,DeploymentConnectorPolicy
from workbench_agent.persistence import schema


async def require_connector_authority(uow,context,run_id,identities):
    policy=DeploymentConnectorPolicy(lambda:identities)
    try:
        if context.graph_version in ('dod.v13','dod.v14','dod.v15'):
            from workbench_agent.application.synthetic_source_policy import require_bound_source
            identity=await require_bound_source(uow,context,run_id)
            await policy.require_source_current(context,identity)
        else:
            await policy.require_current(context)
    except (CapabilityBlocked,KeyError,TypeError):
        raise DomainError('OPERATION_DENIED',403) from None

"""Revalidate action provenance before any delivery or reconciliation I/O."""
from workbench_agent.runtime.graphs.ids import GraphId
import json
from workbench_agent.application.authorization import Authorization
from workbench_agent.application.errors import DomainError
from workbench_agent.application.idempotency import digest
from workbench_agent.domain.actions import action_payload_hash
from workbench_agent.domain.context import RunContext
from workbench_agent.domain.identity import AuthContext
from workbench_agent.domain.profiles import VersionRef
from workbench_agent.tools.policy import load_published_binding


async def require_action_authority(backend,uow,action,approval,run,*,reader=None):
    try:
        context=RunContext.model_validate_json(run['versions'])
        origin=AuthContext(team_id=context.team_id,subject=context.subject,roles=context.roles,
            identity_kind=context.identity_kind,agent_scope=context.agent_scope)
        await Authorization(backend).current(uow,origin,run['agent_id'],'operator')
        scope=await backend.runs.current_scope(uow,context.team_id,run['run_id'])
        payload=json.loads(action['payload_ref'])
        ref=VersionRef.model_validate(payload['binding'])
        row=await backend.configuration.one(uow,'tool_bindings',team_id=context.team_id,agent_id=context.agent_id,
            binding_id=ref.resource_id,version=int(ref.version),hash=ref.content_hash)
        if not scope or scope['scope_id']!=action['scope'] or not row or ref not in context.tools: raise ValueError()
        raw=json.loads(row['schema'])
        binding=load_published_binding(raw) if digest(raw)==ref.content_hash else None
        from workbench_agent.application.action_grants import matched_action_grant
        grant,policy_hash,policy=await matched_action_grant(backend,uow,context,ref,payload['arguments'])
        from workbench_agent.tools.permissions import DeploymentToolPermissions
        permissions=DeploymentToolPermissions(lambda:policy['tool_permissions'])
        if context.identity_kind=='connector':
            from workbench_agent.application.connector_authority import require_connector_authority
            await require_connector_authority(uow,context,run['run_id'],policy['connector_identities'])
        if not binding or binding.read_only or not permissions or not permissions.allows(context,binding.permission,'diagnosis'):
            raise ValueError()
        if reader is not None:
            await Authorization(backend).current(uow,reader,run['agent_id'],'operator')
            if reader.identity_kind=='connector': raise ValueError()
            reader_context=context.model_copy(update={'subject':reader.subject,'identity_kind':reader.identity_kind,
                'agent_scope':reader.agent_scope,'roles':reader.roles})
            if not permissions.allows(reader_context,binding.permission,'diagnosis'): raise ValueError()
        if context.graph_id==GraphId.RCA_PROPOSAL:
            from workbench_agent.application.synthetic_proposal_policy import bound_rule
            rule,proposal_revision,proposal_hash,source_revision,source_hash=await bound_rule(backend,uow,context,run['run_id'],action['scope'],with_policy=True)
            from sqlalchemy import select
            from workbench_agent.persistence import schema
            linked=(await uow.execute(select(schema.synthetic_action_policy_bindings).where(schema.synthetic_action_policy_bindings.c.team_id==context.team_id,schema.synthetic_action_policy_bindings.c.action_id==action['action_id']))).mappings().first()
            if not linked or (linked['proposal_revision'],linked['proposal_hash'],linked['source_revision'],linked['source_hash'])!=(proposal_revision,proposal_hash,source_revision,source_hash): raise ValueError()
            if rule['binding']!=ref.model_dump(mode='json') or rule['arguments']!=payload['arguments'] or rule['approver']!=grant['approver']:
                raise ValueError()
        if context.graph_id==GraphId.RCA_BATCH_PROPOSAL:
            from workbench_agent.application.synthetic_batch_policy import bound_rules
            rules,proposal_revision,proposal_hash,source_revision,source_hash=await bound_rules(backend,uow,context,run['run_id'],action['scope'],with_policy=True)
            prefix='synthetic-v15:'+run['run_id']+':'
            if not action['action_key'].startswith(prefix): raise ValueError()
            selected=[rule for rule in rules if rule['rule_id']==action['action_key'][len(prefix):]]
            if len(selected)!=1: raise ValueError()
            rule=selected[0]
            from sqlalchemy import select
            from workbench_agent.persistence import schema
            linked=(await uow.execute(select(schema.synthetic_action_policy_bindings).where(schema.synthetic_action_policy_bindings.c.team_id==context.team_id,schema.synthetic_action_policy_bindings.c.action_id==action['action_id']))).mappings().first()
            if not linked or (linked['proposal_revision'],linked['proposal_hash'],linked['source_revision'],linked['source_hash'])!=(proposal_revision,proposal_hash,source_revision,source_hash): raise ValueError()
            if rule['binding']!=ref.model_dump(mode='json') or rule['arguments']!=payload['arguments'] or rule['approver']!=grant['approver'] or rule['target']!=payload['target']:
                raise ValueError()
        target={'server':binding.server,'credential_scope':binding.credential_scope,'tool_name':binding.tool_name}
        version=f'{ref.resource_id}:{ref.version}:{ref.content_hash}'
        approver=AuthContext(team_id=context.team_id,subject=grant['approver'],roles=(),identity_kind='user')
        current_approver=await Authorization(backend).current(uow,approver,run['agent_id'],'operator')
        approver_context=context.model_copy(update={'subject':grant['approver'],'identity_kind':'user',
            'agent_scope':None,'roles':current_approver.roles})
        if not permissions.allows(approver_context,binding.permission,'diagnosis'): raise ValueError()
        if (payload['target']!=target or action['tool_version']!=version or approval['tool_version']!=version
                or approval['args_hash']!=action['payload_hash'] or approval['policy_hash']!=policy_hash
                or approval['subject']!=grant['approver']
                or action_payload_hash(version,target,payload['arguments'])!=action['payload_hash']): raise ValueError()
        return payload,binding,context
    except DomainError:
        raise
    except Exception:
        raise DomainError('OPERATION_DENIED',403) from None

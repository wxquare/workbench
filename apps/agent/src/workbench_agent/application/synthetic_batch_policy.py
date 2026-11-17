"""CAS authority for at most four independently approved local mock actions."""
import json,re
from sqlalchemy import select,insert,update
from workbench_agent.application.errors import DomainError
from workbench_agent.application.idempotency import canonical,digest
from workbench_agent.application.synthetic_proposal_policy import validate_rule
from workbench_agent.persistence import schema

MAX_RULES_PER_SCOPE=4
MAX_TOTAL_RULES=256
_TARGETS={'local.mock.jira':'CreateTicket','local.mock.seatalk':'SendNotification'}


def validate_batch_rule(rule):
    try:
        if not isinstance(rule,dict) or set(rule)!={'rule_id','source','scope','binding','target','arguments','approver'}: raise ValueError()
        if not isinstance(rule['rule_id'],str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,48}',rule['rule_id']): raise ValueError()
        validate_rule({k:v for k,v in rule.items() if k not in ('rule_id','target')})
        target=rule['target']
        if not isinstance(target,dict) or set(target)!={'server','credential_scope','tool_name'}: raise ValueError()
        if target['server'] not in _TARGETS or target['credential_scope']!='local-mock' or target['tool_name']!=_TARGETS[target['server']]: raise ValueError()
        return rule
    except (ValueError,TypeError,KeyError,DomainError): raise DomainError('INVALID_BATCH_POLICY',422) from None


def validate_collection(rules):
    if not isinstance(rules,list) or len(rules)>MAX_TOTAL_RULES: raise DomainError('INVALID_BATCH_POLICY',422)
    values=sorted([validate_batch_rule(rule) for rule in rules],key=lambda r:r['rule_id'])
    signatures=[canonical({k:r[k] for k in ('source','scope','target','arguments')}) for r in values]
    groups={}
    for rule in values:
        key=canonical({'source':rule['source'],'scope':rule['scope']})
        groups[key]=groups.get(key,0)+1
    if (len({r['rule_id'] for r in values})!=len(values) or len(set(signatures))!=len(values)
        or any(count>MAX_RULES_PER_SCOPE for count in groups.values()) or len(canonical(values).encode())>1048576):
        raise DomainError('INVALID_BATCH_POLICY',422)
    return values


class SyntheticBatchPolicy:
    def __init__(self,backend): self.backend=backend

    async def sync(self,rules,*,expected_revision):
        if type(expected_revision) is not int or expected_revision<0: raise DomainError('INVALID_BATCH_POLICY',422)
        values=validate_collection(rules)
        table=schema.synthetic_batch_policy
        async with self.backend.create_uow().begin_write() as uow:
            row=(await uow.execute(select(table).where(table.c.policy_id=='deployment'))).mappings().first()
            if (row['revision'] if row else 0)!=expected_revision: raise DomainError('BATCH_POLICY_CONFLICT',409)
            now=await self.backend.clock.now_ms(uow)
            data=dict(revision=expected_revision+1,policy_json=canonical(values),policy_hash=digest(values),updated_at=now)
            if row:
                changed=await uow.execute(update(table).where(table.c.policy_id=='deployment',table.c.revision==expected_revision).values(**data))
                if changed.rowcount!=1: raise DomainError('BATCH_POLICY_CONFLICT',409)
            else: await uow.execute(insert(table).values(policy_id='deployment',**data))
            return data['revision']


async def matching_rules(uow,source,scope,*,with_policy=False):
    table=schema.synthetic_batch_policy
    row=(await uow.execute(select(table).where(table.c.policy_id=='deployment'))).mappings().first()
    try:
        rules=json.loads(row['policy_json']) if row else None
        if not isinstance(rules,list) or digest(rules)!=row['policy_hash']: raise ValueError()
        rules=validate_collection(rules)
        selected=sorted([rule for rule in rules if rule['source']==source and rule['scope']==scope],key=lambda r:r['rule_id'])
        return (selected,row['revision'],row['policy_hash']) if with_policy else selected
    except (ValueError,TypeError,KeyError,AttributeError,DomainError): raise DomainError('OPERATION_DENIED',403) from None


async def bound_rules(backend,uow,context,run_id,scope_id,*,with_policy=False):
    from types import SimpleNamespace
    from workbench_agent.application.synthetic_source_policy import require_bound_source
    from workbench_agent.incidents.synthetic import source_scope
    await require_bound_source(uow,context,run_id)
    source,event,scope,matches=await source_scope(backend,uow,SimpleNamespace(team_id=context.team_id,run_id=run_id))
    if not matches or not scope or scope['scope_id']!=scope_id: raise DomainError('OPERATION_DENIED',403)
    selector={'team_id':context.team_id,'agent_id':context.agent_id,'connector_id':source['connector_id'],
        'identity_id':source['identity_id'],'subject':context.subject,'service':event.service,
        'environment':event.environment,'region':event.region,'rule':event.rule}
    matched=await matching_rules(uow,selector,json.loads(scope['scope_json']),with_policy=with_policy)
    if not with_policy: return matched
    source_policy=(await uow.execute(select(schema.synthetic_source_policy).where(schema.synthetic_source_policy.c.policy_id=='deployment'))).mappings().one()
    return (*matched,source_policy['revision'],source_policy['policy_hash'])

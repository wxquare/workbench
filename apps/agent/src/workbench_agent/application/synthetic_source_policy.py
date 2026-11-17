"""Explicit CAS-managed source authority; absence and bad state deny v13."""
import json
from sqlalchemy import select,insert,update
from workbench_agent.application.idempotency import canonical,digest
from workbench_agent.application.errors import DomainError
from workbench_agent.persistence import schema


class SyntheticSourcePolicy:
    def __init__(self,backend): self.backend=backend

    async def sync(self,grants,*,expected_revision,connector_identities=None):
        if type(expected_revision) is not int or expected_revision<0 or not isinstance(grants,list):
            raise DomainError('INVALID_SOURCE_POLICY',422)
        allowed={'team_id','agent_id','connector_id','identity_id','subject','service','environment','region','rule'}
        if any(not isinstance(g,dict) or set(g)!=allowed or any(not isinstance(v,str) or not v.strip() or v!=v.strip() or len(v)>128 for v in g.values()) for g in grants):
            raise DomainError('INVALID_SOURCE_POLICY',422)
        values=sorted(grants,key=canonical)
        if connector_identities is None: connector_identities={}
        if not isinstance(connector_identities,dict) or any(
            not isinstance(key,str) or not key.strip() or key!=key.strip() or len(key)>128 or
            not isinstance(item,dict) or set(item)!={'subject','team_id','agent_ids'} or
            not isinstance(item['subject'],str) or not item['subject'].strip() or
            not isinstance(item['team_id'],str) or not item['team_id'].strip() or
            not isinstance(item['agent_ids'],(list,tuple)) or not item['agent_ids'] or
            any(not isinstance(w,str) or not w.strip() for w in item['agent_ids'])
            for key,item in connector_identities.items()): raise DomainError('INVALID_SOURCE_POLICY',422)
        document={'grants':values,'connector_identities':connector_identities}
        if len({canonical(v) for v in values})!=len(values) or len(canonical(document).encode())>1048576:
            raise DomainError('INVALID_SOURCE_POLICY',422)
        table=schema.synthetic_source_policy
        async with self.backend.create_uow().begin_write() as uow:
            row=(await uow.execute(select(table).where(table.c.policy_id=='deployment'))).mappings().first()
            if (row['revision'] if row else 0)!=expected_revision: raise DomainError('SOURCE_POLICY_CONFLICT',409)
            now=await self.backend.clock.now_ms(uow)
            data=dict(revision=expected_revision+1,policy_hash=digest(document),policy_json=canonical(document),updated_at=now)
            if row:
                changed=await uow.execute(update(table).where(table.c.policy_id=='deployment',table.c.revision==expected_revision).values(**data))
                if changed.rowcount!=1: raise DomainError('SOURCE_POLICY_CONFLICT',409)
            else: await uow.execute(insert(table).values(policy_id='deployment',**data))
            return data['revision']


async def require_source_grant(uow,grant):
    table=schema.synthetic_source_policy
    row=(await uow.execute(select(table).where(table.c.policy_id=='deployment'))).mappings().first()
    try:
        policy=json.loads(row['policy_json']) if row else None
        if not isinstance(policy,dict) or set(policy)!={'grants','connector_identities'} or digest(policy)!=row['policy_hash']: raise ValueError()
        grants,identities=policy['grants'],policy['connector_identities']
        identity=identities.get(grant['identity_id']) if isinstance(identities,dict) else None
        if (not isinstance(grants,list) or grant not in grants or not isinstance(identity,dict)
            or identity.get('subject')!=grant['subject'] or identity.get('team_id')!=grant['team_id']
            or grant['agent_id'] not in identity.get('agent_ids',[])): raise ValueError()
    except (ValueError,TypeError,KeyError): raise DomainError('OPERATION_DENIED',403) from None


async def require_bound_source(uow,context,run_id):
    table=schema.synthetic_alert_sources
    row=(await uow.execute(select(table).where(table.c.team_id==context.team_id,table.c.run_id==run_id))).mappings().first()
    try:
        payload=json.loads(row['payload']) if row else None
        if not row or not row['identity_id'] or digest(payload)!=row['payload_hash'] or context.identity_kind!='connector': raise ValueError()
        grant={'team_id':context.team_id,'agent_id':context.agent_id,'connector_id':row['connector_id'],
            'identity_id':row['identity_id'],'subject':context.subject,**{key:payload[key] for key in ('service','environment','region','rule')}}
    except (ValueError,TypeError,KeyError): raise DomainError('OPERATION_DENIED',403) from None
    await require_source_grant(uow,grant)
    return row['identity_id']

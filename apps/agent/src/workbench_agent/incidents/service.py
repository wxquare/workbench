"""Tenant-scoped incident access with current source and scope checks."""
import json
from types import SimpleNamespace
from sqlalchemy import select, and_, or_
from workbench_agent.application.authorization import Authorization
from workbench_agent.application.errors import DomainError,not_found
from workbench_agent.incidents.synthetic import source_scope
from workbench_agent.persistence import schema


class IncidentService:
    def __init__(self,backend,settings):
        self.backend,self.settings=backend,settings
        self.authorization=Authorization(backend)

    async def _delivery_links(self,uow,auth,incident,run):
        links=schema.incident_links; bindings=schema.incident_action_links
        rows=(await uow.execute(select(links,bindings.c.action_id).join(bindings,and_(
            links.c.team_id==bindings.c.team_id,links.c.link_id==bindings.c.link_id)).where(
            links.c.team_id==auth.team_id,links.c.incident_id==incident['incident_id']).order_by(links.c.created_at,links.c.link_id))).mappings()
        result=[]
        for link in rows:
            try:
                if link['run_id']!=run['run_id'] or link['external_system'] not in ('local.mock.jira','local.mock.dod','local.mock.seatalk'): continue
                action=await self.backend.actions.one(uow,'actions',team_id=auth.team_id,action_id=link['action_id'])
                if not action or action['run_id']!=run['run_id'] or action['state']!='succeeded': continue
                approval=await self.backend.actions.current_approval(uow,auth.team_id,action['action_id'])
                outbox=await self.backend.actions.one(uow,'outbox',team_id=auth.team_id,action_id=action['action_id'])
                if not approval or approval['decision']!='approve' or not outbox or outbox['status']!='succeeded' or outbox['destination']!=link['external_system']: continue
                from workbench_agent.delivery.local_simulator import expected_external_id
                if link['external_id']!=expected_external_id(action['action_id'],link['external_system'].removeprefix('local.mock.')): continue
                fact={'external_id':link['external_id']}
                if json.loads(action['receipt'])!=fact or json.loads(outbox['receipt_ref'])!=fact: continue
                from workbench_agent.application.action_authority import require_action_authority
                await require_action_authority(self.backend,uow,action,approval,run,reader=auth)
                result.append({'action_id':action['action_id'],'external_system':link['external_system'],'external_id':link['external_id']})
            except (DomainError,ValueError,TypeError,KeyError):
                continue
        return result

    async def _project(self,uow,auth,row,run):
        try:
            await self.authorization.current(uow,auth,run['agent_id'])
            if row['source']!='synthetic-alert-v2': raise not_found()
            source,event,scope,matches=await source_scope(self.backend,uow,SimpleNamespace(team_id=auth.team_id,run_id=run['run_id']))
            binding=self.settings.service.synthetic_ingress.get(source['connector_id'])
            if (not matches or not binding or binding.protocol!='synthetic-alert-v2'
                or binding.team_id!=auth.team_id or binding.agent_id!=run['agent_id']
                or (event.service,event.environment,event.region,event.rule) not in binding.allowed_scopes): raise not_found()
            from workbench_agent.domain.context import RunContext
            context=RunContext.model_validate_json(run['versions'])
            if context.identity_kind!='connector' or context.subject!=binding.actor_subject: raise not_found()
            from workbench_agent.application.synthetic_source_policy import require_bound_source
            await require_bound_source(uow,context,run['run_id'])
            identity=self.settings.service.connector_identities.get(source['identity_id'])
            if (source['identity_id']!=binding.identity_id or not identity
                or identity['subject']!=binding.actor_subject or identity['team_id']!=auth.team_id
                or run['agent_id'] not in identity['agent_ids']): raise not_found()
            if not await self.backend.configuration.one(uow,'team_members',team_id=auth.team_id,
                subject=binding.actor_subject,status='active'): raise not_found()
            proof=json.loads(row['evidence']); conclusion=json.loads(row['conclusion'])
            if (proof!={'scope_id':scope['scope_id'],'source_hash':source['payload_hash']}
                or conclusion!={'rule':event.rule,'status':'alert_observed'}
                or row['service']!=event.service or row['severity']!='unknown'): raise not_found()
            return {'incident_id':row['incident_id'],'run_id':run['run_id'],
                'source':row['source'],'service':row['service'],'severity':row['severity'],
                'conclusion':conclusion,'scope':json.loads(scope['scope_json']),
                'delivery_links':await self._delivery_links(uow,auth,row,run),
                'links':{'run':f"/v1/runs/{run['run_id']}"}}
        except (DomainError,ValueError,TypeError,KeyError):
            raise not_found() from None

    async def get(self,auth,incident_id):
        async with self.backend.create_uow().begin_read() as uow:
            row=(await uow.execute(select(schema.incidents).where(schema.incidents.c.team_id==auth.team_id,
                schema.incidents.c.incident_id==incident_id))).mappings().first()
            if not row: raise not_found()
            run=await self.backend.runs.one(uow,'runs',team_id=auth.team_id,run_id=row['run_id'])
            if not run: raise not_found()
            return await self._project(uow,auth,row,run)

    async def list(self,auth,agent_id,limit,after):
        async with self.backend.create_uow().begin_read() as uow:
            await self.authorization.current(uow,auth,agent_id)
            i,r=schema.incidents,schema.runs
            query=select(i).join(r,and_(i.c.team_id==r.c.team_id,i.c.run_id==r.c.run_id)).where(
                i.c.team_id==auth.team_id,r.c.agent_id==agent_id)
            if after:
                cursor=(await uow.execute(select(i).join(r,and_(i.c.team_id==r.c.team_id,i.c.run_id==r.c.run_id)).where(
                    i.c.team_id==auth.team_id,r.c.agent_id==agent_id,i.c.incident_id==after))).mappings().first()
                if not cursor: raise not_found()
                run=await self.backend.runs.one(uow,'runs',team_id=auth.team_id,run_id=cursor['run_id'])
                await self._project(uow,auth,cursor,run)
                query=query.where(or_(i.c.created_at>cursor['created_at'],and_(i.c.created_at==cursor['created_at'],i.c.incident_id>cursor['incident_id'])))
            items=[]
            while True:
                rows=(await uow.execute(query.order_by(i.c.created_at,i.c.incident_id).limit(100))).mappings().all()
                if not rows: break
                for row in rows:
                    run=await self.backend.runs.one(uow,'runs',team_id=auth.team_id,run_id=row['run_id'])
                    try: item=await self._project(uow,auth,row,run)
                    except DomainError: continue
                    items.append(item)
                    if len(items)>limit:
                        return {'items':items[:limit],'next_cursor':items[limit-1]['incident_id']}
                if len(rows)<100: break
                last=rows[-1]
                query=query.where(or_(i.c.created_at>last['created_at'],and_(i.c.created_at==last['created_at'],i.c.incident_id>last['incident_id'])))
            return {'items':items,'next_cursor':None}

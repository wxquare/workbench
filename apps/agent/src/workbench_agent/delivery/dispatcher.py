"""Conservative outbox dispatcher. No sender is configured by default.

A committed sending marker precedes every I/O. Expired sending leases and
ambiguous exceptions are unknown, never automatically retried.
"""
import json
import asyncio
from uuid import uuid4
from workbench_agent.domain.context import RunContext
from workbench_agent.domain.identity import AuthContext
from workbench_agent.domain.profiles import VersionRef
from workbench_agent.application.authorization import Authorization
from workbench_agent.application.idempotency import digest
from workbench_agent.application.errors import DomainError
from workbench_agent.domain.actions import action_payload_hash
from workbench_agent.tools.policy import load_published_binding


class DeliveryNotSent(Exception):
    """Trusted adapter proves no external request was submitted."""


class Dispatcher:
    def __init__(self, backend, worker_id, *, lease_ms=30000):
        if not isinstance(worker_id,str) or not worker_id or not 1000<=lease_ms<=60000:
            raise ValueError('invalid delivery worker')
        self.backend, self.worker_id, self.lease_ms = backend, worker_id, lease_ms

    async def dispatch_one(self):
        senders=getattr(self.backend,'_delivery_senders',None) or {}
        async with self.backend.create_uow().begin_write() as uow:
            now=await self.backend.clock.now_ms(uow)
            for old in await self.backend.actions.expired(uow,now):
                state='unknown' if old['status']=='sending' else 'pending'
                await self.backend.actions.change(uow,'outbox',{'team_id':old['team_id'],'outbox_id':old['outbox_id'],
                    'revision':old['revision'],'status':old['status']}, {'status':state,'lease_owner':None,'lease_until':None,
                    'revision':old['revision']+1,'updated_at':now})
                if state=='unknown':
                    await self.backend.actions.change(uow,'actions',{'team_id':old['team_id'],'action_id':old['action_id'],'state':'authorized'},
                        {'state':'unknown','updated_at':now})
            row=await self.backend.actions.candidate(uow,now,tuple(senders))
            if not row: return None
            action=await self.backend.actions.one(uow,'actions',team_id=row['team_id'],action_id=row['action_id'])
            if not action or action['state']!='authorized': return None
            approval=await self.backend.actions.current_approval(uow,row['team_id'],row['action_id'])
            run=await self.backend.runs.one(uow,'runs',team_id=row['team_id'],run_id=action['run_id'])
            if run and RunContext.model_validate_json(run['versions']).graph_version in ('dod.v14','dod.v15'):
                from workbench_agent.domain.guards import TERMINAL_RUN_STATUSES
                if run['status'] not in TERMINAL_RUN_STATUSES: return None
                from sqlalchemy import select
                from workbench_agent.persistence import schema
                incident=(await uow.execute(select(schema.incidents.c.incident_id).where(schema.incidents.c.team_id==row['team_id'],
                    schema.incidents.c.run_id==run['run_id'],schema.incidents.c.source=='synthetic-alert-v2'))).first()
                if run['status'] not in ('succeeded','partial') or not incident:
                    if not await self.backend.actions.change(uow,'outbox',{'team_id':row['team_id'],'outbox_id':row['outbox_id'],
                        'status':'pending','revision':row['revision']},{'status':'dead','revision':row['revision']+1,'updated_at':now}): raise DomainError('ACTION_CONFLICT',409)
                    if not await self.backend.actions.change(uow,'actions',{'team_id':row['team_id'],'action_id':action['action_id'],'state':'authorized'},
                        {'state':'failed','updated_at':now}): raise DomainError('ACTION_CONFLICT',409)
                    await self.backend.configuration.insert(uow,'audit_logs',dict(audit_id=str(uuid4()),team_id=row['team_id'],
                        actor=self.worker_id,object=action['action_id'],operation='action.delivery.blocked',decision='denied',
                        trace='{"reason":"INCIDENT_UNAVAILABLE"}',created_at=now,updated_at=now))
                    await self.backend.execution_events.append_control(uow,row['team_id'],action['run_id'],'delivery.updated',
                        {'action_id':action['action_id'],'outbox_id':row['outbox_id'],'state':'dead'})
                    return 'denied'
            try:
                if not approval or not run or approval['decision']!='approve' or approval['expires_at']<=now: raise ValueError()
                from workbench_agent.application.action_authority import require_action_authority
                payload,binding,_=await require_action_authority(self.backend,uow,action,approval,run)
                if row['destination']!=binding.server or row['payload_ref']!=action['payload_ref']: raise ValueError()
            except (ValueError,TypeError,KeyError,DomainError):
                await self.backend.actions.change(uow,'outbox',{'team_id':row['team_id'],'outbox_id':row['outbox_id'],'status':'pending'},
                    {'status':'dead','revision':row['revision']+1,'updated_at':now})
                await self.backend.actions.change(uow,'actions',{'team_id':row['team_id'],'action_id':row['action_id'],'state':'authorized'},
                    {'state':'failed','updated_at':now})
                return 'denied'
            revision=row['revision']+1
            changed=await self.backend.actions.change(uow,'outbox',{'team_id':row['team_id'],'outbox_id':row['outbox_id'],
                'status':'pending','revision':row['revision']},{'status':'sending','lease_owner':self.worker_id,
                'lease_until':now+self.lease_ms,'attempt':row['attempt']+1,'revision':revision,'updated_at':now})
            if not changed: return None
        # The sending commit must happen before invoking any external adapter.
        try:
            async with asyncio.timeout(self.lease_ms / 2000):
                receipt=await senders[row['destination']].send(payload,action['action_id'])
            if not isinstance(receipt,dict) or set(receipt)!={'external_id'} or not isinstance(receipt['external_id'],str) or not 1<=len(receipt['external_id'])<=128:
                raise ValueError('invalid receipt')
            result='succeeded'
        except DeliveryNotSent:
            receipt=None
            result='dead' if row['attempt']+1 >= 8 else 'retry'
        except Exception:
            receipt=None
            result='unknown'
        async with self.backend.create_uow().begin_write() as uow:
            now=await self.backend.clock.now_ms(uow)
            current=await self.backend.actions.one(uow,'outbox',team_id=row['team_id'],outbox_id=row['outbox_id'])
            if (not current or current['status']!='sending' or current['revision']!=revision
                    or current['lease_owner']!=self.worker_id or current['lease_until']<=now):
                return 'unknown'
            from workbench_agent.application.idempotency import canonical
            encoded=canonical(receipt) if receipt else None
            backoffs=(2000,10000,30000,120000,600000,1800000,7200000)
            outbox_state='pending' if result=='retry' else result
            action_state='authorized' if result=='retry' else ('failed' if result=='dead' else result)
            values={'status':outbox_state,'receipt_ref':encoded,'lease_owner':None,'lease_until':None,
                'revision':revision+1,'updated_at':now}
            if result=='retry': values['available_at']=now+backoffs[row['attempt']]
            if not await self.backend.actions.change(uow,'outbox',{'team_id':row['team_id'],'outbox_id':row['outbox_id'],
                'status':'sending','revision':revision,'lease_owner':self.worker_id},values): raise DomainError('ACTION_CONFLICT',409)
            if not await self.backend.actions.change(uow,'actions',{'team_id':row['team_id'],'action_id':row['action_id'],'state':'authorized'},
                {'state':action_state,'receipt':encoded,'updated_at':now}): raise DomainError('ACTION_CONFLICT',409)
            if result=='succeeded':
                from workbench_agent.incidents.delivery import project_delivery_link
                await project_delivery_link(self.backend,uow,action,row,receipt,now)
            await self.backend.configuration.insert(uow,'audit_logs',dict(audit_id=str(uuid4()),team_id=row['team_id'],
                actor=self.worker_id,object=row['action_id'],operation='action.delivery',decision=result,trace='{}',created_at=now,updated_at=now))
            await self.backend.execution_events.append_control(uow,row['team_id'],action['run_id'],'delivery.updated',
                {'action_id':row['action_id'],'outbox_id':row['outbox_id'],'state':result})
        return result

"""Reconcile ambiguous delivery only from a deployment-owned read-only lookup."""
import json
from workbench_agent.application.authorization import Authorization
from workbench_agent.application.errors import DomainError, not_found
from workbench_agent.application.idempotency import canonical


class ActionReconciler:
    def __init__(self, backend):
        self.backend=backend

    async def _load(self,uow,auth,action_id):
        action=await self.backend.actions.one(uow,'actions',team_id=auth.team_id,action_id=action_id)
        if not action: raise not_found()
        run=await self.backend.runs.one(uow,'runs',team_id=auth.team_id,run_id=action['run_id'])
        if not run: raise not_found()
        await Authorization(self.backend).current(uow,auth,run['agent_id'],'operator')
        if auth.identity_kind=='connector': raise DomainError('OPERATION_DENIED',403)
        if action['state']!='unknown': raise DomainError('ACTION_NOT_UNKNOWN',409)
        approval=await self.backend.actions.current_approval(uow,auth.team_id,action_id)
        outbox=await self.backend.actions.one(uow,'outbox',team_id=auth.team_id,action_id=action_id)
        if not approval or not outbox or outbox['status']!='unknown' or outbox['payload_ref']!=action['payload_ref']:
            raise DomainError('ACTION_INCONSISTENT',409)
        from workbench_agent.application.action_authority import require_action_authority
        payload,binding,_=await require_action_authority(self.backend,uow,action,approval,run,reader=auth)
        if approval['decision']!='approve' or outbox['destination']!=binding.server:
            raise DomainError('ACTION_INCONSISTENT',409)
        return action,outbox

    async def reconcile(self,auth,action_id):
        async with self.backend.create_uow().begin_read() as uow:
            action,outbox=await self._load(uow,auth,action_id)
            payload=json.loads(action['payload_ref'])
            destination=outbox['destination']
        connector=(getattr(self.backend,'_delivery_senders',None) or {}).get(destination)
        if not connector or not callable(getattr(connector,'lookup',None)):
            raise DomainError('RECONCILIATION_UNAVAILABLE',503)
        try:
            fact=await connector.lookup(payload,action_id)
            if fact is None: return {'action_id':action_id,'state':'unknown'}
            if not isinstance(fact,dict) or set(fact)!={'external_id'} or not isinstance(fact['external_id'],str) or not 1<=len(fact['external_id'])<=128:
                raise ValueError()
        except Exception:
            raise DomainError('RECONCILIATION_UNAVAILABLE',503) from None
        async with self.backend.create_uow().begin_write() as uow:
            latest,current=await self._load(uow,auth,action_id)
            if latest['payload_hash']!=action['payload_hash'] or current['revision']!=outbox['revision']:
                raise DomainError('ACTION_CONFLICT',409)
            now=await self.backend.clock.now_ms(uow)
            encoded=canonical(fact)
            if not await self.backend.actions.change(uow,'outbox',{'team_id':auth.team_id,'outbox_id':outbox['outbox_id'],
                    'status':'unknown','revision':outbox['revision']},{'status':'succeeded','receipt_ref':encoded,
                    'revision':outbox['revision']+1,'updated_at':now}): raise DomainError('ACTION_CONFLICT',409)
            if not await self.backend.actions.change(uow,'actions',{'team_id':auth.team_id,'action_id':action_id,'state':'unknown'},
                    {'state':'succeeded','receipt':encoded,'updated_at':now}): raise DomainError('ACTION_CONFLICT',409)
            from workbench_agent.incidents.delivery import project_delivery_link
            await project_delivery_link(self.backend,uow,latest,current,fact,now)
        return {'action_id':action_id,'state':'succeeded','receipt':fact}

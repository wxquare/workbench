"""Guarded action proposals and explicit approval; delivery remains disabled."""
import json
from uuid import uuid4
from workbench_agent.application.authorization import Authorization
from workbench_agent.application.errors import DomainError, not_found
from workbench_agent.application.idempotency import canonical, digest
from workbench_agent.domain.actions import action_payload_hash
from workbench_agent.domain.context import RunContext
from workbench_agent.runtime.budgets.service import BudgetService
from workbench_agent.tools.policy import arguments_within_scope, load_published_binding, validate_arguments


_BATCH_PROPOSAL=object()

class ActionService:
    def __init__(self, backend):
        self.backend = backend
        self.auth = Authorization(backend)

    async def propose(self, execution, action_key, binding_ref, scope_id, args, *, _synthetic_rule=None):
        from workbench_agent.jobs.worker import Execution, Worker
        owner = getattr(execution, '_tool_owner', None)
        if not isinstance(execution, Execution) or not isinstance(owner, Worker) or owner.backend is not self.backend or not owner._owns_tool_execution(execution):
            raise DomainError('STALE_EXECUTION', 409)
        if not isinstance(action_key, str) or not 1 <= len(action_key) <= 128:
            raise DomainError('INVALID_REQUEST', 422)
        await execution.check_operation()
        guard = execution.guard
        async with self.backend.create_uow().begin_write() as uow:
            return await self._propose_in(uow,execution,action_key,binding_ref,scope_id,args,_synthetic_rule)

    async def _propose_in(self,uow,execution,action_key,binding_ref,scope_id,args,_synthetic_rule,_batch_capability=None):
        from workbench_agent.jobs.worker import Execution,Worker
        owner=getattr(execution,'_tool_owner',None)
        if not isinstance(execution,Execution) or not isinstance(owner,Worker) or owner.backend is not self.backend or not owner._owns_tool_execution(execution): raise DomainError('STALE_EXECUTION',409)
        if not isinstance(action_key,str) or not 1<=len(action_key)<=128: raise DomainError('INVALID_REQUEST',422)
        guard=execution.guard
        budgets=BudgetService(self.backend)
        run, context = await budgets._owned(uow, guard)
        await budgets._dispatch_deadline(uow,guard,run)
        if execution.claim.context != context or binding_ref not in context.tools:
            raise not_found()
        row = await self.backend.configuration.one(uow, 'tool_bindings', team_id=context.team_id,
            agent_id=context.agent_id, binding_id=binding_ref.resource_id,
            version=int(binding_ref.version), hash=binding_ref.content_hash)
        if not row: raise not_found()
        raw = json.loads(row['schema'])
        if digest(raw) != binding_ref.content_hash: raise not_found()
        binding = load_published_binding(raw)
        if not binding or binding.read_only or not execution.tool_permissions.allows(context, binding.permission, execution._expert_name):
            raise DomainError('OPERATION_DENIED', 403)
        scope = await self.backend.runs.current_scope(uow, guard.team_id, guard.run_id)
        if not scope or scope['scope_id'] != scope_id: raise DomainError('STALE_QUERY_SCOPE', 409)
        if not validate_arguments(binding, args) or not arguments_within_scope(args, json.loads(scope['scope_json'])):
            raise DomainError('INVALID_ACTION', 422)
        from workbench_agent.application.action_grants import matched_action_grant
        from workbench_agent.domain.identity import AuthContext
        grant,policy_hash,policy=await matched_action_grant(self.backend,uow,context,binding_ref,args)
        if context.graph_version=='dod.v14':
            from workbench_agent.application.synthetic_proposal_policy import bound_rule
            rule,proposal_revision,proposal_hash,source_revision,source_hash=await bound_rule(self.backend,uow,context,guard.run_id,scope_id,with_policy=True)
            if (rule!=_synthetic_rule or rule['binding']!=binding_ref.model_dump(mode='json')
                or rule['arguments']!=args or rule['approver']!=grant['approver']
                or action_key!='synthetic-v14:'+guard.run_id): raise DomainError('OPERATION_DENIED',403)
        if context.graph_version=='dod.v15':
            if _batch_capability is not _BATCH_PROPOSAL: raise DomainError('OPERATION_DENIED',403)
            from workbench_agent.application.synthetic_batch_policy import bound_rules
            rules,proposal_revision,proposal_hash,source_revision,source_hash=await bound_rules(self.backend,uow,context,guard.run_id,scope_id,with_policy=True)
            if _synthetic_rule not in rules or _synthetic_rule['binding']!=binding_ref.model_dump(mode='json') or _synthetic_rule['arguments']!=args or _synthetic_rule['approver']!=grant['approver'] or action_key!='synthetic-v15:'+guard.run_id+':'+_synthetic_rule['rule_id']:
                raise DomainError('OPERATION_DENIED',403)
            if _synthetic_rule['target']!={'server':binding.server,'credential_scope':binding.credential_scope,'tool_name':binding.tool_name}: raise DomainError('OPERATION_DENIED',403)
        approver=AuthContext(team_id=context.team_id,subject=grant['approver'],roles=(),identity_kind='user')
        current_approver=await self.auth.current(uow,approver,context.agent_id,'operator')
        from workbench_agent.tools.permissions import DeploymentToolPermissions
        active_permissions=DeploymentToolPermissions(lambda:policy['tool_permissions'])
        if not active_permissions.allows(context,binding.permission,execution._expert_name): raise DomainError('OPERATION_DENIED',403)
        approver_context=context.model_copy(update={'subject':grant['approver'],'identity_kind':'user',
            'agent_scope':None,'roles':current_approver.roles})
        if not active_permissions.allows(approver_context,binding.permission,execution._expert_name): raise DomainError('OPERATION_DENIED',403)
        if context.identity_kind=='connector':
            from workbench_agent.application.connector_authority import require_connector_authority
            await require_connector_authority(uow,context,guard.run_id,policy['connector_identities'])
        tool_version = f'{binding_ref.resource_id}:{binding_ref.version}:{binding_ref.content_hash}'
        target = {'server':binding.server, 'credential_scope':binding.credential_scope, 'tool_name':binding.tool_name}
        payload_hash = action_payload_hash(tool_version, target, args)
        prior = await self.backend.actions.one(uow, 'actions', team_id=guard.team_id, action_key=action_key)
        if prior:
            if context.graph_version in ('dod.v14','dod.v15'):
                from sqlalchemy import select
                from workbench_agent.persistence import schema
                linked=(await uow.execute(select(schema.synthetic_action_policy_bindings).where(schema.synthetic_action_policy_bindings.c.team_id==guard.team_id,schema.synthetic_action_policy_bindings.c.action_id==prior['action_id']))).mappings().first()
                if not linked or (linked['proposal_revision'],linked['proposal_hash'],linked['source_revision'],linked['source_hash'])!=(proposal_revision,proposal_hash,source_revision,source_hash): raise DomainError('OPERATION_DENIED',403)
            if prior['run_id'] != guard.run_id or prior['payload_hash'] != payload_hash or prior['scope'] != scope_id:
                raise DomainError('ACTION_CONFLICT', 409)
            approval = await self.backend.actions.current_approval(uow,guard.team_id,prior['action_id'])
            return {'action_id':prior['action_id'], 'approval_id':approval['approval_id'], 'state':prior['state'], 'payload_hash':payload_hash}
        now = await self.backend.clock.now_ms(uow)
        action_id, approval_id = str(uuid4()), str(uuid4())
        await self.backend.actions.insert(uow, 'actions', dict(team_id=guard.team_id, action_id=action_id,
            run_id=guard.run_id, action_key=action_key, tool_version=tool_version, payload_hash=payload_hash,
            payload_ref=canonical({'binding':binding_ref.model_dump(mode='json'), 'target':target, 'arguments':args}),
            scope=scope_id, state='proposed', revision=0, created_at=now, updated_at=now))
        if context.graph_version in ('dod.v14','dod.v15'):
            from sqlalchemy import insert
            from workbench_agent.persistence import schema
            await uow.execute(insert(schema.synthetic_action_policy_bindings).values(team_id=guard.team_id,action_id=action_id,
                proposal_revision=proposal_revision,proposal_hash=proposal_hash,source_revision=source_revision,source_hash=source_hash))
        await self.backend.actions.insert(uow, 'approvals', dict(team_id=guard.team_id, approval_id=approval_id,
            run_id=guard.run_id, action_id=action_id, tool_version=tool_version, target=canonical(target), args_hash=payload_hash,
            policy_hash=policy_hash, subject=grant['approver'], expires_at=now+30*60*1000, decision='pending',
            created_at=now, updated_at=now))
        await self.backend.configuration.insert(uow,'audit_logs',dict(audit_id=str(uuid4()),team_id=guard.team_id,
            actor=context.subject,object=action_id,operation='action.propose',decision='allowed',trace='{}',created_at=now,updated_at=now))
        await self.backend.execution_events.append_execution(uow,guard,'action.updated',{'action_id':action_id,'state':'proposed'})
        return {'action_id':action_id, 'approval_id':approval_id, 'state':'proposed', 'payload_hash':payload_hash}

    async def propose_synthetic(self,execution,scope_id):
        from workbench_agent.application.synthetic_proposal_policy import bound_rule
        from workbench_agent.domain.profiles import VersionRef
        await execution.check_operation()
        async with self.backend.create_uow().begin_read() as uow:
            rule=await bound_rule(self.backend,uow,execution.claim.context,execution.guard.run_id,scope_id)
        return await self.propose(execution,'synthetic-v14:'+execution.guard.run_id,
            VersionRef.model_validate(rule['binding']),scope_id,rule['arguments'],_synthetic_rule=rule)

    async def propose_synthetic_batch(self,execution,scope_id):
        from workbench_agent.jobs.worker import Execution,Worker
        from workbench_agent.application.synthetic_batch_policy import bound_rules
        from workbench_agent.domain.profiles import VersionRef
        owner=getattr(execution,'_tool_owner',None)
        if not isinstance(execution,Execution) or not isinstance(owner,Worker) or owner.backend is not self.backend or not owner._owns_tool_execution(execution):
            raise DomainError('STALE_EXECUTION',409)
        await execution.check_operation()
        async with self.backend.create_uow().begin_write() as uow:
            rules=await bound_rules(self.backend,uow,execution.claim.context,execution.guard.run_id,scope_id)
            return [await self._propose_in(uow,execution,'synthetic-v15:'+execution.guard.run_id+':'+rule['rule_id'],
                VersionRef.model_validate(rule['binding']),scope_id,rule['arguments'],rule,_BATCH_PROPOSAL) for rule in rules]

    async def authorize(self, auth, approval_id, decision, expected_args_hash):
        if decision not in ('approve', 'reject'): raise DomainError('INVALID_REQUEST', 422)
        async with self.backend.create_uow().begin_write() as uow:
            approval = await self.backend.actions.one(uow, 'approvals', team_id=auth.team_id, approval_id=approval_id)
            if not approval: raise not_found()
            action = await self.backend.actions.one(uow, 'actions', team_id=auth.team_id, action_id=approval['action_id'])
            run = await self.backend.runs.one(uow, 'runs', team_id=auth.team_id, run_id=approval['run_id'])
            if not action or not run: raise not_found()
            current_approval=await self.backend.actions.current_approval(uow,auth.team_id,action['action_id'])
            if not current_approval or current_approval['approval_id']!=approval_id: raise DomainError('APPROVAL_CONFLICT',409)
            await self.auth.current(uow, auth, run['agent_id'], 'operator')
            if auth.identity_kind == 'connector' or auth.subject != approval['subject']:
                raise DomainError('OPERATION_DENIED', 403)
            if expected_args_hash != approval['args_hash'] or expected_args_hash != action['payload_hash']:
                raise DomainError('APPROVAL_CONFLICT', 409)
            if approval['decision'] != 'pending':
                if approval['decision'] != decision: raise DomainError('APPROVAL_CONFLICT', 409)
                return {'action_id':action['action_id'],'approval_id':approval_id,
                    'state':'authorized' if decision=='approve' else 'rejected','payload_hash':action['payload_hash']}
            now = await self.backend.clock.now_ms(uow)
            if action['state'] not in ('proposed','reapproval_pending'): raise DomainError('APPROVAL_CONFLICT', 409)
            if now >= approval['expires_at']: raise DomainError('APPROVAL_EXPIRED', 409)
            scope = await self.backend.runs.current_scope(uow, auth.team_id, run['run_id'])
            if not scope or scope['scope_id'] != action['scope']: raise DomainError('STALE_QUERY_SCOPE', 409)
            if expected_args_hash != approval['args_hash'] or expected_args_hash != action['payload_hash']:
                raise DomainError('APPROVAL_CONFLICT', 409)
            payload=json.loads(action['payload_ref'])
            context=RunContext.model_validate_json(run['versions'])
            from workbench_agent.domain.profiles import VersionRef
            ref=VersionRef.model_validate(payload['binding'])
            row=await self.backend.configuration.one(uow, 'tool_bindings', team_id=auth.team_id,agent_id=run['agent_id'],binding_id=ref.resource_id,version=int(ref.version),hash=ref.content_hash)
            if not row or ref not in context.tools or digest(json.loads(row['schema'])) != ref.content_hash:
                raise DomainError('OPERATION_DENIED',403)
            binding=load_published_binding(json.loads(row['schema']))
            if not binding or binding.read_only:
                raise DomainError('OPERATION_DENIED',403)
            from workbench_agent.application.action_authority import require_action_authority
            await require_action_authority(self.backend,uow,action,approval,run,reader=auth)
            if action_payload_hash(action['tool_version'], payload['target'], payload['arguments']) != action['payload_hash']:
                raise DomainError('APPROVAL_CONFLICT',409)
            state='authorized' if decision=='approve' else 'rejected'
            await self.backend.actions.change(uow,'approvals',{'team_id':auth.team_id,'approval_id':approval_id,'decision':'pending'}, {'decision':decision,'updated_at':now})
            if not await self.backend.actions.change(uow,'actions',{'team_id':auth.team_id,'action_id':action['action_id'],'state':action['state'],'revision':action['revision']}, {'state':state,'revision':action['revision']+1,'updated_at':now}):
                raise DomainError('ACTION_CONFLICT',409)
            if decision=='approve':
                if action['state']=='reapproval_pending':
                    outbox=await self.backend.actions.one(uow,'outbox',team_id=auth.team_id,action_id=action['action_id'])
                    if not outbox or outbox['status']!='dead' or outbox['receipt_ref'] is not None: raise DomainError('ACTION_CONFLICT',409)
                    if not await self.backend.actions.change(uow,'outbox',{'team_id':auth.team_id,'outbox_id':outbox['outbox_id'],
                            'status':'dead','revision':outbox['revision']},{'status':'pending','available_at':now,
                            'revision':outbox['revision']+1,'updated_at':now}): raise DomainError('ACTION_CONFLICT',409)
                else:
                    await self.backend.actions.insert(uow,'outbox',dict(team_id=auth.team_id,outbox_id=str(uuid4()),action_id=action['action_id'],
                        destination=binding.server,payload_ref=action['payload_ref'],status='pending',available_at=now,attempt=0,revision=0,created_at=now,updated_at=now))
            await self.backend.configuration.insert(uow,'audit_logs',dict(audit_id=str(uuid4()),team_id=auth.team_id,
                actor=auth.subject,object=action['action_id'],operation='action.authorize',decision=decision,trace='{}',created_at=now,updated_at=now))
            await self.backend.execution_events.append_control(uow,auth.team_id,run['run_id'],'action.updated',{'action_id':action['action_id'],'state':state})
            return {'action_id':action['action_id'],'approval_id':approval_id,'state':state,'payload_hash':action['payload_hash']}

    async def get(self, auth, action_id):
        async with self.backend.create_uow().begin_read() as uow:
            action=await self.backend.actions.one(uow,'actions',team_id=auth.team_id,action_id=action_id)
            if not action: raise not_found()
            run=await self.backend.runs.one(uow,'runs',team_id=auth.team_id,run_id=action['run_id'])
            if not run: raise not_found()
            await self.auth.current(uow,auth,run['agent_id'],'operator')
            approval=await self.backend.actions.current_approval(uow,auth.team_id,action_id)
            if not approval or auth.identity_kind=='connector' or auth.subject!=approval['subject']: raise not_found()
            from workbench_agent.application.action_authority import require_action_authority
            payload,_,_=await require_action_authority(self.backend,uow,action,approval,run,reader=auth)
            scope=await self.backend.runs.current_scope(uow,auth.team_id,run['run_id'])
            return {'action_id':action_id,'state':action['state'],'approval_id':approval['approval_id'],
                'payload_hash':action['payload_hash'],'target':payload['target'],'arguments':payload['arguments'],
                'binding':payload['binding'],'scope':json.loads(scope['scope_json']),'expires_at':approval['expires_at']}

    async def retry(self,auth,action_id,*,key,receipt=False):
        """One explicit retry after proven-not-sent exhaustion; never retry unknown."""
        from workbench_agent.application.action_authority import require_action_authority
        from workbench_agent.application.idempotency import Idempotency
        async with self.backend.create_uow().begin_write() as uow:
            action=await self.backend.actions.one(uow,'actions',team_id=auth.team_id,action_id=action_id)
            if not action: raise not_found()
            run=await self.backend.runs.one(uow,'runs',team_id=auth.team_id,run_id=action['run_id'])
            approval=await self.backend.actions.current_approval(uow,auth.team_id,action_id)
            if not run or not approval: raise not_found()
            await self.auth.current(uow,auth,run['agent_id'],'operator')
            if auth.identity_kind=='connector' or auth.subject!=approval['subject']: raise not_found()
            await require_action_authority(self.backend,uow,action,approval,run,reader=auth)
            async def operation():
                outbox=await self.backend.actions.one(uow,'outbox',team_id=auth.team_id,action_id=action_id)
                now=await self.backend.clock.now_ms(uow)
                if approval['decision']!='approve' or now>=approval['expires_at']: raise DomainError('APPROVAL_EXPIRED',409)
                if (action['state']!='failed' or not outbox or outbox['status']!='dead'
                        or outbox['attempt']<8 or outbox['receipt_ref'] is not None):
                    raise DomainError('ACTION_RETRY_UNAVAILABLE',409)
                if not await self.backend.actions.change(uow,'outbox',{'team_id':auth.team_id,'outbox_id':outbox['outbox_id'],
                        'status':'dead','revision':outbox['revision']}, {'status':'pending','available_at':now,
                        'revision':outbox['revision']+1,'updated_at':now}): raise DomainError('ACTION_CONFLICT',409)
                if not await self.backend.actions.change(uow,'actions',{'team_id':auth.team_id,'action_id':action_id,
                        'state':'failed','revision':action['revision']},{'state':'authorized','revision':action['revision']+1,
                        'updated_at':now}): raise DomainError('ACTION_CONFLICT',409)
                await self.backend.execution_events.append_control(uow,auth.team_id,run['run_id'],'action.updated',
                    {'action_id':action_id,'state':'authorized'})
                return {'action_id':action_id,'state':'authorized'}
            result=await Idempotency(self.backend).execute(uow,auth,run['agent_id'],'action.retry',key,
                {'action_id':action_id},operation,status=202)
        return result if receipt else result.body

    async def reapprove(self,auth,action_id,expected_args_hash,*,key,receipt=False):
        """Request a fresh named approval; never extend an expired approval."""
        from workbench_agent.application.action_authority import require_action_authority
        from workbench_agent.application.idempotency import Idempotency
        async with self.backend.create_uow().begin_write() as uow:
            action=await self.backend.actions.one(uow,'actions',team_id=auth.team_id,action_id=action_id)
            if not action: raise not_found()
            run=await self.backend.runs.one(uow,'runs',team_id=auth.team_id,run_id=action['run_id'])
            approval=await self.backend.actions.current_approval(uow,auth.team_id,action_id)
            if not run or not approval: raise not_found()
            await self.auth.current(uow,auth,run['agent_id'],'operator')
            if auth.identity_kind=='connector' or auth.subject!=approval['subject']: raise not_found()
            await require_action_authority(self.backend,uow,action,approval,run,reader=auth)
            if expected_args_hash!=action['payload_hash'] or expected_args_hash!=approval['args_hash']:
                raise DomainError('APPROVAL_CONFLICT',409)
            async def operation():
                now=await self.backend.clock.now_ms(uow)
                outbox=await self.backend.actions.one(uow,'outbox',team_id=auth.team_id,action_id=action_id)
                if approval['decision']!='approve' or now<approval['expires_at']:
                    raise DomainError('REAPPROVAL_UNAVAILABLE',409)
                if (action['state']!='failed' or not outbox or outbox['status']!='dead'
                        or outbox['receipt_ref'] is not None): raise DomainError('REAPPROVAL_UNAVAILABLE',409)
                approval_id=str(uuid4())
                await self.backend.actions.insert(uow,'approvals',dict(team_id=auth.team_id,approval_id=approval_id,
                    run_id=run['run_id'],action_id=action_id,tool_version=approval['tool_version'],target=approval['target'],
                    args_hash=approval['args_hash'],policy_hash=approval['policy_hash'],subject=approval['subject'],
                    expires_at=now+30*60*1000,decision='pending',created_at=max(now,approval['created_at']+1),updated_at=now))
                if not await self.backend.actions.change(uow,'actions',{'team_id':auth.team_id,'action_id':action_id,
                        'state':'failed','revision':action['revision']},{'state':'reapproval_pending',
                        'revision':action['revision']+1,'updated_at':now}): raise DomainError('ACTION_CONFLICT',409)
                await self.backend.execution_events.append_control(uow,auth.team_id,run['run_id'],'approval.required',
                    {'action_id':action_id,'approval_id':approval_id,'args_hash':expected_args_hash})
                return {'action_id':action_id,'approval_id':approval_id,'state':'reapproval_pending','payload_hash':expected_args_hash}
            result=await Idempotency(self.backend).execute(uow,auth,run['agent_id'],'action.reapprove',key,
                {'action_id':action_id,'expected_args_hash':expected_args_hash},operation,status=202)
        return result if receipt else result.body

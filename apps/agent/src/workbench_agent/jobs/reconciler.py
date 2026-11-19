"""Checkpoint-driven recovery. Inspection outside writes; every decision fenced inside."""
from workbench_agent.runtime.graphs.ids import GraphId
from dataclasses import dataclass
from workbench_agent.application.results import ResultService
from workbench_agent.application.resume import ResumeApplier
from workbench_agent.domain.guards import TERMINAL_RUN_STATUSES
from workbench_agent.runtime.results import saved_outcome

@dataclass(frozen=True)
class RecoveryDecision:
    run_id: str
    action: str
    reason: str | None = None
    checkpoint_id: str | None = None

@dataclass(frozen=True)
class Observation:
    checkpoint_id: str | None
    next_nodes: tuple[str,...]
    valid: bool

class Reconciler:
    def __init__(self,backend,registry,worker_id='reconciler'):
        self.backend,self.registry,self.worker_id=backend,registry,worker_id
        self.connector_policy=None

    async def prepare(self,claim):
        """Native locked-framework state semantics, never under caller write UoW."""
        executor=self.registry._executors.get(claim.context.graph_id)
        try:
            graph=executor.registry.resolve(claim.context.graph_id,executor.registry.state_schema_version(claim.context.graph_id))
            with executor.guard_provider.bind(claim.guard):
                snapshot=await graph.aget_state({'configurable':{'thread_id':claim.guard.run_id}})
            checkpoint_id=(snapshot.config or {}).get('configurable',{}).get('checkpoint_id')
            return Observation(checkpoint_id,tuple(snapshot.next),True)
        except (ValueError,KeyError,AttributeError,TypeError):
            return Observation(None,(),False)

    async def recover(self,run_id):
        async with self.backend.create_uow().begin_write() as uow:
            run=await self.backend.runs.one(uow,'runs',run_id=run_id)
            if run is None: return RecoveryDecision(run_id,'missing')
            if run['status'] in TERMINAL_RUN_STATUSES: return RecoveryDecision(run_id,'terminal')
            candidates=await self.backend.jobs.candidates(uow,run_id=run_id)
            claim,_=await self.backend.jobs.claim(uow,self.registry.capabilities(),self.worker_id,candidates,projection=True)
        if claim is None: return RecoveryDecision(run_id,'unavailable','LIVE_OWNER_OR_CAPABILITY_BLOCKED')
        from workbench_agent.jobs.worker import Execution,CapabilityBlocked
        from workbench_agent.application.errors import DomainError
        if await self._cancel(claim): return RecoveryDecision(run_id,'cancelled')
        try:
            await Execution(self.backend,claim,self.connector_policy).check_projection()
        except (CapabilityBlocked,DomainError):
            # Current permissions are execution authority, never restored by a
            # checkpoint. Park under the actual new guard and free the quota so
            # one revoked Run cannot stop the scheduler's remaining candidates.
            async with self.backend.create_uow().begin_write() as uow:
                await self.backend.jobs.release(uow,claim.guard,status='queued',job_status='capability_blocked',
                    recovery_phase='projection',error_code='EXECUTION_CAPABILITY_LOST')
            return RecoveryDecision(run_id,'unavailable','EXECUTION_CAPABILITY_LOST')
        observation=await self.prepare(claim)
        async with self.backend.create_uow().begin_write() as uow:
            return await self.reconcile(uow,claim,observation)

    async def _cancel(self,claim):
        async with self.backend.create_uow().begin_write() as uow:
            if await self.backend.controls.status(uow,claim.guard.team_id,claim.guard.run_id)!='cancel_requested': return False
            await self.backend.controls.finish_cancel(uow,claim.guard)
            return True

    async def reconcile(self,uow,claim,observation=None):
        guard=claim.guard; store=self.backend.runs
        await self.backend.jobs.lock_scope(uow,guard)
        await self.backend.lease_guard.require(uow,guard,{'running','finalizing'})
        run=await store.one(uow,'runs',run_id=guard.run_id)
        checkpoint_id=None
        saved=None
        invalid=False
        try:
            saved=await store.checkpoint(uow,guard.team_id,guard.run_id)
            checkpoint_id=saved['checkpoint_id'] if saved else None
        except (ValueError,TypeError,KeyError): invalid=True
        def decision(action,reason=None): return RecoveryDecision(guard.run_id,action,reason,checkpoint_id)
        async def fail(reason):
            await self.backend.jobs.release(uow,guard,status='failed',error_code=reason)
            return decision('failed',reason)
        if invalid: return await fail('STATE_RECONCILIATION_REQUIRED')
        now=await self.backend.clock.now_ms(uow)
        if run['wall_deadline_at']<=now:
            await self.backend.controls.finish(uow,run,'expired'); return decision('expired')
        if observation is None or not observation.valid or observation.checkpoint_id!=checkpoint_id:
            return await fail('STATE_RECONCILIATION_REQUIRED')
        try: outcome=saved_outcome(saved,claim.context.graph_id)
        except (ValueError,TypeError,KeyError): return await fail('STATE_RECONCILIATION_REQUIRED')
        pending=await store.resume_pending(uow,guard.team_id,guard.run_id)
        applied=False
        if pending and saved:
            try: resume=ResumeApplier().apply(pending,saved)
            except (ValueError,KeyError,TypeError): return await fail('STATE_RECONCILIATION_REQUIRED')
            applied=resume.already_applied
            if applied:
                await store.change(uow,'resume_requests',{'request_id':pending['request_id']},{'status':'applied','updated_at':now})
                await store.change(uow,'interrupts',{'interrupt_id':pending['interrupt_id'],'accepted_resume_request_id':pending['request_id']},{'status':'consumed','updated_at':now})
        if outcome is not None:
            from workbench_agent.runtime.graphs.evidence_authority import sanitize_v8_outcome
            if claim.context.graph_id not in (GraphId.RCA_SYNTHETIC,GraphId.RCA_PROPOSAL,GraphId.RCA_BATCH_PROPOSAL):
                outcome=await sanitize_v8_outcome(self.backend,uow,claim,outcome)
            from workbench_agent.application.errors import DomainError
            if claim.context.graph_id not in (GraphId.RCA_SYNTHETIC,GraphId.RCA_PROPOSAL,GraphId.RCA_BATCH_PROPOSAL):
                try: await ResultService(self.backend).validate(uow,guard,outcome)
                except (ValueError,DomainError): return await fail('STATE_RECONCILIATION_REQUIRED')
            try:
                await ResultService(self.backend).finalize_in(uow,guard,outcome,outcome.digest())
            except DomainError as error:
                if claim.context.graph_id not in (GraphId.RCA_SYNTHETIC,GraphId.RCA_PROPOSAL,GraphId.RCA_BATCH_PROPOSAL) or error.code!='OPERATION_DENIED': raise
                await self.backend.jobs.release(uow,guard,status='queued',job_status='capability_blocked',
                    recovery_phase='projection',error_code='EXECUTION_CAPABILITY_LOST')
                return decision('unavailable','EXECUTION_CAPABILITY_LOST')
            return decision('finalized')
        if saved and saved['interrupts'] and pending is None:
            if claim.context.graph_id not in (GraphId.RCA_SYNTHETIC,GraphId.RCA_PROPOSAL,GraphId.RCA_BATCH_PROPOSAL): return await fail('STATE_RECONCILIATION_REQUIRED')
            # Only registered, exact named descriptors can become a public wait.
            try: await self.registry._projectors[claim.context.graph_id].project(uow,claim,None)
            except (ValueError,KeyError,TypeError): return await fail('STATE_RECONCILIATION_REQUIRED')
            return decision('waiting')
        if run['recovery_phase']=='finalizing' or run['status']=='finalizing':
            return await fail('STATE_RECONCILIATION_REQUIRED')
        if claim.segment_attempt>=3: return await fail('RETRY_EXHAUSTED')
        if run['deadline_at'] is not None and run['deadline_at']<=now:
            await self.backend.controls.finish(uow,run,'timed_out'); return decision('timed_out')
        allowed={
            GraphId.KNOWLEDGE_SCOPED_EXCERPT:{'prepare_context','route','select_skills','collect_baseline','investigate','validate_evidence','compose_outcome','finalize'},
            GraphId.CONVERSATION_SINGLE_TURN:{'answer'},
            GraphId.CONVERSATION_MULTI_TURN:{'answer'},
            GraphId.KNOWLEDGE_CITATION_REVIEW:{'prepare','retrieve','draft_answer','validate_citations','review_support','record_gap','compose_outcome'},
            GraphId.RCA_BATCH_PROPOSAL:{'prepare','input','scope','bind_baseline','collect_baseline','propose','compose'},
            GraphId.RCA_PROPOSAL:{'prepare','input','scope','bind_baseline','collect_baseline','propose','compose'},
            GraphId.RCA_SYNTHETIC:{'prepare','input','scope','bind_baseline','collect_baseline','compose'},
            GraphId.SOP_CONDITIONAL:{'prepare','step','compose'}}
        safe=(saved is None or bool(observation.next_nodes) and set(observation.next_nodes)<=allowed.get(claim.context.graph_id,set()))
        has_actions=await store.has_actions(uow,guard.team_id,guard.run_id)
        if safe and has_actions and claim.context.graph_id==GraphId.RCA_PROPOSAL:
            from sqlalchemy import select
            from workbench_agent.persistence import schema
            actions=(await uow.execute(select(schema.actions).where(schema.actions.c.team_id==guard.team_id,
                schema.actions.c.run_id==guard.run_id))).mappings().all()
            if len(actions)==1 and actions[0]['action_key']=='synthetic-v14:'+guard.run_id and actions[0]['state'] in ('proposed','authorized','rejected'):
                action=actions[0]
                approval=await self.backend.actions.current_approval(uow,guard.team_id,action['action_id'])
                outbox=await self.backend.actions.one(uow,'outbox',team_id=guard.team_id,action_id=action['action_id'])
                try:
                    proposed=action['state']=='proposed' and approval and approval['decision']=='pending' and outbox is None
                    authorized=(action['state']=='authorized' and approval and approval['decision']=='approve' and outbox
                        and outbox['status']=='pending' and outbox['attempt']==0 and outbox['receipt_ref'] is None)
                    rejected=action['state']=='rejected' and approval and approval['decision']=='reject' and outbox is None
                    if not (proposed or authorized or rejected): raise DomainError('OPERATION_DENIED',403)
                    from workbench_agent.application.action_authority import require_action_authority
                    await require_action_authority(self.backend,uow,action,approval,run)
                    has_actions=False
                except DomainError:
                    await self.backend.jobs.release(uow,guard,status='queued',job_status='capability_blocked',
                        recovery_phase='projection',error_code='EXECUTION_CAPABILITY_LOST')
                    return decision('unavailable','EXECUTION_CAPABILITY_LOST')
        if safe and has_actions and claim.context.graph_id==GraphId.RCA_BATCH_PROPOSAL:
            from sqlalchemy import select
            from workbench_agent.persistence import schema
            from workbench_agent.application.synthetic_batch_policy import bound_rules
            from workbench_agent.application.action_authority import require_action_authority
            actions=(await uow.execute(select(schema.actions).where(schema.actions.c.team_id==guard.team_id,
                schema.actions.c.run_id==guard.run_id))).mappings().all()
            try:
                if not actions or len({action['scope'] for action in actions})!=1: raise DomainError('OPERATION_DENIED',403)
                rules=await bound_rules(self.backend,uow,claim.context,guard.run_id,actions[0]['scope'])
                expected={'synthetic-v15:'+guard.run_id+':'+rule['rule_id'] for rule in rules}
                if not expected or {action['action_key'] for action in actions}!=expected or len(actions)!=len(expected):
                    raise DomainError('OPERATION_DENIED',403)
                for action in actions:
                    approval=await self.backend.actions.current_approval(uow,guard.team_id,action['action_id'])
                    outbox=await self.backend.actions.one(uow,'outbox',team_id=guard.team_id,action_id=action['action_id'])
                    proposed=action['state']=='proposed' and approval and approval['decision']=='pending' and outbox is None
                    rejected=action['state']=='rejected' and approval and approval['decision']=='reject' and outbox is None
                    authorized=(action['state']=='authorized' and approval and approval['decision']=='approve' and outbox
                        and outbox['status']=='pending' and outbox['attempt']==0 and outbox['receipt_ref'] is None)
                    if not (proposed or rejected or authorized): raise DomainError('OPERATION_DENIED',403)
                    await require_action_authority(self.backend,uow,action,approval,run)
                has_actions=False
            except DomainError:
                await self.backend.jobs.release(uow,guard,status='queued',job_status='capability_blocked',
                    recovery_phase='projection',error_code='EXECUTION_CAPABILITY_LOST')
                return decision('unavailable','EXECUTION_CAPABILITY_LOST')
        if not safe or has_actions:
            return await fail('STATE_RECONCILIATION_REQUIRED')
        await self.backend.jobs.continue_recovery(uow,guard,checkpoint_id)
        return decision('continue','APPLIED_RESUME' if applied else 'SAFE_FOUNDATION_NEXT')

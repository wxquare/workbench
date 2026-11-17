"""Caller-UoW accounting. Commit mark_sent BEFORE executing network I/O.

Each reservation is one real network attempt, not a Job/SDK retry count. A
cached receipt reuses its invocation ID; every actual retry allocates a new ID.
Marking possibly_sent conservatively consumes that attempt even on a crash.
Only an unsent reservation can be released. Unknown usage retains its token
hold until a monotonic provider receipt fills the unknown fields.
"""
import json
from dataclasses import asdict
from uuid import uuid4
from workbench_agent.application.authorization import Authorization
from workbench_agent.application.errors import DomainError,not_found
from workbench_agent.application.idempotency import canonical,digest
from workbench_agent.domain.context import RunContext
from workbench_agent.domain.identity import AuthContext
from workbench_agent.domain.transitions import active_elapsed
from workbench_agent.domain.usage import ReservationRequest,Reservation,ActualUsage,UsageRecord,UsageSummary
from workbench_agent.runtime.budgets.limits import has_capacity

class BudgetService:
    def __init__(self,backend,*,summary_tokens:int|None=None):
        if summary_tokens is not None and (type(summary_tokens) is not int or summary_tokens<=0):
            raise ValueError('summary token holdback must be positive')
        self.backend,self.store=backend,backend.usage
        self.authorization=Authorization(backend)
        self.summary_tokens=summary_tokens

    async def _run(self,uow,team_id,run_id):
        run=await self.backend.runs.one(uow,'runs',team_id=team_id,run_id=run_id)
        if not run: raise not_found()
        return run

    async def _owned(self,uow,guard):
        await self.backend.lease_guard.require(uow,guard)
        run=await self._run(uow,guard.team_id,guard.run_id)
        context=RunContext.model_validate_json(run['versions'])
        auth=AuthContext(team_id=context.team_id,subject=context.subject,identity_kind=context.identity_kind,
            roles=context.roles,agent_scope=context.agent_scope)
        await self.authorization.current(uow,auth,run['agent_id'],'operator')
        return run,context

    async def _dispatch_deadline(self,uow,guard,run):
        # Dispatch is stricter than accounting: finalizing may settle old work
        # but must never issue a new network attempt.
        await self.backend.lease_guard.require(uow,guard,{'running'})
        now=await self.backend.clock.now_ms(uow)
        elapsed=active_elapsed(run,now,await self.backend.runs.active_until(uow,guard.team_id,guard.run_id))
        if elapsed>=json.loads(run['budgets'])['wall_seconds']*1000 or now>=run['wall_deadline_at']:
            raise DomainError('BUDGET_EXCEEDED',409)
        return now

    async def _model(self,uow,context,role):
        ref=context.model_for(role)
        from workbench_agent.application.profiles import ProfileService
        auth=AuthContext(team_id=context.team_id,subject=context.subject,roles=context.roles,
            identity_kind=context.identity_kind,agent_scope=context.agent_scope)
        return (await ProfileService(self.backend).model_version(uow,auth,ref)).configuration

    @staticmethod
    def _reservation(row,*,dispatch_allowed=False):
        return Reservation(reservation_id=row['reservation_id'],invocation_id=row['invocation'],kind=row['kind'],
            status=row['status'],send_state=row['send_state'],dispatch_allowed=dispatch_allowed,model_attempts=row['model_attempts'],
            tool_attempts=row['tool_attempts'],token_upper_bound=row['tokens'])

    @staticmethod
    def _record(row):
        values={key:row[key] for key in ActualUsage.model_fields}
        if values['pricing_version']=='unknown' and values['amount_micros'] is None: values['pricing_version']=None
        return UsageRecord(record_id=row['record_id'],invocation_id=row['invocation'],
            usage_pending=bool(row['usage_pending']),cost_pending=bool(row['cost_pending']),**values)

    async def _entry(self,uow,guard,invocation):
        row=await self.store.reservation(uow,guard.team_id,guard.run_id,invocation)
        if not row: raise not_found()
        return row

    async def _scope_id(self,uow,guard):
        row=await self.backend.runs.current_scope(uow,guard.team_id,guard.run_id)
        return row['scope_id'] if row else None

    async def reserve(self,guard,request:ReservationRequest,*,uow) -> Reservation:
        run,context=await self._owned(uow,guard)
        old=await self.store.reservation(uow,guard.team_id,guard.run_id,request.invocation_id)
        if old:
            if old['request_hash']!=digest(request.model_dump(mode='json')):
                raise DomainError('RESERVATION_CONFLICT',409)
            return self._reservation(old)
        limits=json.loads(run['budgets'])
        now=await self._dispatch_deadline(uow,guard,run)
        if request.model_attempts:
            role='summary' if request.kind=='summary' else ('evaluation' if request.kind=='evaluation' else request.role)
            model=await self._model(uow,context,role)
            if request.token_upper_bound>model.max_context_tokens:
                raise DomainError('MAX_CONTEXT_EXCEEDED',422)
        rows=await self.store.reservations(uow,guard.team_id,guard.run_id)
        summary_held=any(r['kind']=='summary' and r['status']!='cancelled' for r in rows)
        summary_model=await self._model(uow,context,'summary')
        policy=await self.store.policy(uow,guard.team_id,guard.run_id)
        if policy and self.summary_tokens is not None and self.summary_tokens!=policy['summary_tokens']:
            raise DomainError('BUDGET_POLICY_CONFLICT',409)
        if policy is None:
            error=DomainError('BUDGET_POLICY_UNINITIALIZED',409)
            error.links={'create_run':'/v1/runs'}
            error.add_note('Historical summary policy is unknown. Do not infer or reset it: create a replacement Run, or obtain reviewed offline reconciliation supported by authoritative evidence. See docs/runbooks/budget-policy.md.')
            raise error
        token_hold=policy['summary_tokens']
        if token_hold>summary_model.max_context_tokens:
            raise DomainError('SUMMARY_RESERVE_INVALID',422)
        if request.kind=='summary' and request.token_upper_bound>token_hold:
            raise DomainError('SUMMARY_RESERVE_REQUIRED',422)
        tokens_reserved=token_hold if request.kind=='summary' else request.token_upper_bound
        hold_calls=0 if request.kind=='summary' or summary_held else 1
        hold_tokens=0 if request.kind=='summary' or summary_held else token_hold
        summary=await self._snapshot(uow,run)
        # Unknown usage holds the complete reservation, including partial known
        # tokens. Do not add those partial values twice.
        records=await self.store.records(uow,guard.team_id,guard.run_id)
        settled=sum(r['input_tokens']+r['output_tokens'] for r in records if not r['usage_pending'])
        if not (has_capacity(limits['model_calls'],summary.model_attempts,summary.reserved_model_attempts,
                request.model_attempts,hold_calls)
            and has_capacity(limits['tool_calls'],summary.tool_attempts,summary.reserved_tool_attempts,request.tool_attempts)
            and has_capacity(limits['tokens'],settled,summary.reserved_tokens,tokens_reserved,hold_tokens)):
            raise DomainError('BUDGET_EXCEEDED',409)
        values=dict(team_id=guard.team_id,reservation_id=str(uuid4()),run_id=guard.run_id,
            invocation=request.invocation_id,tokens=tokens_reserved,pricing_version='unknown',
            status='reserved',kind=request.kind,request_hash=digest(request.model_dump(mode='json')),
            guard_json=canonical(asdict(guard)),scope_id=await self._scope_id(uow,guard),
            model_attempts=request.model_attempts,tool_attempts=request.tool_attempts,send_state='unsent',created_at=now,updated_at=now)
        await self.store.insert(uow,'usage_reservations',values)
        return self._reservation(values)

    async def mark_sent(self,guard,invocation_id,*,uow,confirmed=False) -> Reservation:
        run,_=await self._owned(uow,guard)
        row=await self._entry(uow,guard,invocation_id)
        if row['status']=='cancelled': raise DomainError('INVOCATION_CANCELLED',409)
        if row['guard_json']!=canonical(asdict(guard)) and row['send_state']=='unsent':
            # A successor must allocate its own invocation; it cannot dispatch a
            # request retained by a previous executor.
            raise DomainError('INVOCATION_OWNER_CHANGED',409)
        if row['scope_id']!=await self._scope_id(uow,guard):
            raise DomainError('STALE_QUERY_SCOPE',409)
        if confirmed and row['send_state']=='unsent':
            raise DomainError('INVOCATION_NOT_SENT',409)
        if row['send_state']!='unsent':
            if confirmed and row['send_state']=='possibly_sent':
                await self.store.change(uow,'usage_reservations',guard.team_id,guard.run_id,invocation_id,
                    {'send_state':'sent','updated_at':await self.backend.clock.now_ms(uow)})
                row=dict(row,send_state='sent')
            # This is a durability replay, never permission to resend network I/O.
            return self._reservation(row)
        await self._dispatch_deadline(uow,guard,run)
        changes=dict(status='sent',send_state='possibly_sent',
            updated_at=await self.backend.clock.now_ms(uow))
        await self.store.change(uow,'usage_reservations',guard.team_id,guard.run_id,invocation_id,changes)
        return self._reservation(dict(row,**changes),dispatch_allowed=True)

    async def release_unsent(self,guard,invocation_id,*,uow) -> Reservation:
        await self._owned(uow,guard)
        row=await self._entry(uow,guard,invocation_id)
        if row['send_state']!='unsent': raise DomainError('INVOCATION_POSSIBLY_SENT',409)
        changes=dict(status='cancelled',updated_at=await self.backend.clock.now_ms(uow))
        await self.store.change(uow,'usage_reservations',guard.team_id,guard.run_id,invocation_id,changes)
        return self._reservation(dict(row,**changes))

    async def settle(self,guard,invocation_id,usage:ActualUsage,*,uow) -> UsageRecord:
        await self._owned(uow,guard)
        return await self._settle(uow,guard,invocation_id,usage)

    async def _settle(self,uow,guard,invocation_id,usage):
        reservation=await self._entry(uow,guard,invocation_id)
        if reservation['send_state']=='unsent': raise DomainError('INVOCATION_NOT_SENT',409)
        old=await self.store.record(uow,guard.team_id,guard.run_id,invocation_id)
        merged=usage.model_dump()
        if old:
            for field in ActualUsage.model_fields:
                previous=old[field]
                if field=='pricing_version' and previous=='unknown' and old['amount_micros'] is None: previous=None
                if previous is not None and merged[field] is not None and previous!=merged[field]:
                    raise DomainError('USAGE_CONFLICT',409)
                if merged[field] is None: merged[field]=previous
        receipt=ActualUsage(**merged)
        usage_pending=receipt.input_tokens is None or receipt.output_tokens is None
        cost_pending=receipt.amount_micros is None
        now=await self.backend.clock.now_ms(uow)
        values=receipt.model_dump()
        values.update(pricing_version=receipt.pricing_version or 'unknown',usage_pending=int(usage_pending),
            cost_pending=int(cost_pending),status='pending' if usage_pending or cost_pending else 'settled',updated_at=now)
        if old:
            await self.store.change(uow,'usage_records',guard.team_id,guard.run_id,invocation_id,values)
            values=dict(old,**values)
        else:
            values.update(team_id=guard.team_id,record_id=str(uuid4()),run_id=guard.run_id,invocation=invocation_id,
                reservation_id=reservation['reservation_id'],created_at=now)
            await self.store.insert(uow,'usage_records',values)
        await self.store.change(uow,'usage_reservations',guard.team_id,guard.run_id,invocation_id,
            dict(status='pending' if usage_pending else 'settled',updated_at=now))
        return self._record(values)

    async def quarantine(self,guard,invocation_id,usage:ActualUsage,*,uow):
        """After STALE_EXECUTION rollback, retain metering only, in a new UoW.

        Does not authorize execution, dispatch, result publication or accounting.
        The full original ownership token must match the durable reservation.
        """
        row=await self._entry(uow,guard,invocation_id)
        if row['guard_json']!=canonical(asdict(guard)) or row['send_state']=='unsent':
            raise not_found()
        hashed=digest(usage.model_dump(mode='json'))
        entries=await self.store.quarantined(uow,guard.team_id,guard.run_id,invocation_id)
        if any(r['receipt_hash']==hashed for r in entries): return
        now=await self.backend.clock.now_ms(uow)
        await self.store.insert(uow,'usage_quarantine',dict(team_id=guard.team_id,quarantine_id=str(uuid4()),
            run_id=guard.run_id,invocation=invocation_id,receipt_hash=hashed,payload=usage.model_dump_json(),
            guard_json=canonical(asdict(guard)),status='quarantined',created_at=now))
        await self.backend.configuration.insert(uow,'audit_logs',dict(team_id=guard.team_id,audit_id=str(uuid4()),
            actor=guard.worker_id,object=invocation_id,operation='usage.quarantine',decision='isolated',
            trace=canonical({'run_id':guard.run_id,'run_epoch':guard.run_epoch,'receipt_hash':hashed}),created_at=now,updated_at=now))

    async def reconcile(self,guard,invocation_id,*,uow) -> UsageRecord:
        await self._owned(uow,guard)
        rows=await self.store.quarantined(uow,guard.team_id,guard.run_id,invocation_id)
        if not rows: raise not_found()
        # Validate the entire set before mutating, even when the caller catches
        # the conflict and commits unrelated work in the same UoW.
        merged={field:None for field in ActualUsage.model_fields}
        for row in rows:
            for field,value in ActualUsage.model_validate_json(row['payload']).model_dump().items():
                if merged[field] is not None and value is not None and merged[field]!=value:
                    raise DomainError('USAGE_CONFLICT',409)
                if value is not None: merged[field]=value
        result=await self._settle(uow,guard,invocation_id,ActualUsage(**merged))
        await self.store.change(uow,'usage_quarantine',guard.team_id,guard.run_id,invocation_id,{'status':'reconciled'})
        now=await self.backend.clock.now_ms(uow)
        await self.backend.configuration.insert(uow,'audit_logs',dict(team_id=guard.team_id,audit_id=str(uuid4()),
            actor=guard.worker_id,object=invocation_id,operation='usage.reconcile',decision='accounted',
            trace=canonical({'run_id':guard.run_id,'run_epoch':guard.run_epoch}),created_at=now,updated_at=now))
        return result

    async def snapshot(self,auth,run_id,*,uow=None) -> UsageSummary:
        if uow is None:
            async with self.backend.create_uow().begin_read() as owned:
                return await self.snapshot(auth,run_id,uow=owned)
        run=await self._run(uow,auth.team_id,run_id)
        await self.authorization.current(uow,auth,run['agent_id'])
        return await self._snapshot(uow,run)

    async def _snapshot(self,uow,run):
        reservations=await self.store.reservations(uow,run['team_id'],run['run_id'])
        records={r['invocation']:r for r in await self.store.records(uow,run['team_id'],run['run_id'])}
        counts=dict(model_attempts=0,tool_attempts=0,reserved_model_attempts=0,reserved_tool_attempts=0,
            reserved_tokens=0,known_tokens=0)
        usage_pending=cost_pending=False
        costs={}
        for entry in reservations:
            if entry['status']=='cancelled': continue
            sent=entry['send_state']!='unsent'
            prefix='' if sent else 'reserved_'
            for kind in ('model_attempts','tool_attempts'): counts[prefix+kind]+=entry[kind]
            receipt=records.get(entry['invocation'])
            if not sent:
                counts['reserved_tokens']+=entry['tokens']
                continue
            unknown=receipt is None or bool(receipt['usage_pending'])
            known=sum(receipt[f] or 0 for f in ('input_tokens','output_tokens')) if receipt else 0
            counts['known_tokens']+=known
            if unknown: counts['reserved_tokens']+=max(entry['tokens'],known)
            usage_pending|=unknown
            cost_pending|=receipt is None or bool(receipt['cost_pending'])
            if receipt and receipt['amount_micros'] is not None:
                currency=receipt['currency'];costs[currency]=costs.get(currency,0)+receipt['amount_micros']
        now=await self.backend.clock.now_ms(uow)
        elapsed=active_elapsed(run,now,await self.backend.runs.active_until(uow,run['team_id'],run['run_id']))
        amount=None if cost_pending or len(costs)>1 else sum(costs.values())
        policy=await self.store.policy(uow,run['team_id'],run['run_id'])
        return UsageSummary(**counts,summary_token_reserve=policy['summary_tokens'] if policy else None,
            total_tokens=None if usage_pending else counts['known_tokens'],
            input_tokens=None if usage_pending else sum(r['input_tokens'] for r in records.values()),
            output_tokens=None if usage_pending else sum(r['output_tokens'] for r in records.values()),
            amount_micros=amount,currency=next(iter(costs)) if len(costs)==1 else None,known_costs=costs,
            usage_pending=usage_pending,cost_pending=cost_pending,active_elapsed_ms=elapsed,
            token_overage=max(0,counts['known_tokens']-json.loads(run['budgets'])['tokens']))

    async def partial_outcome(self,auth,run_id,*,prior=None,uow=None):
        """Deterministic composition when no safe summary call fits the budget.

        Returns an incomplete Outcome; publication still requires ResultService's
        guarded caller-UoW finalization. Existing validated evidence is preserved.
        """
        from workbench_agent.domain.outcomes import RunOutcome,UsageSummary as PublicUsage
        usage=await self.snapshot(auth,run_id,uow=uow)
        public=PublicUsage(status='pending' if usage.usage_pending or usage.cost_pending or usage.currency is None else 'complete',
            model_calls=usage.model_attempts,tool_calls=usage.tool_attempts,
            input_tokens=usage.input_tokens,output_tokens=usage.output_tokens,
            amount_micros=usage.amount_micros if usage.currency else None,
            currency=usage.currency if usage.amount_micros is not None else None)
        unresolved='Model budget cannot safely fund the final summary; analysis remains incomplete.'
        if prior is not None:
            value=RunOutcome.model_validate(prior).model_dump()
            value.update(completed=False,summary=('Budget-limited partial result. '+value['summary'])[:65536],
                unresolved_items=tuple(dict.fromkeys((*value['unresolved_items'],unresolved))),usage_summary=public)
            return RunOutcome.model_validate(value)
        return RunOutcome(kind='analysis.v1',completed=False,summary='Budget-limited partial result. No final model summary was generated.',
            findings=(),root_cause_hypotheses=(),impact=None,evidence_refs=(),unresolved_items=(unresolved,),
            proposed_actions=(),knowledge_gaps=(),usage_summary=public)

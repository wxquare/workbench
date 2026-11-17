"""Guarded reads of Run-pinned repository snapshots with durable evidence."""
import asyncio
import json
from uuid import uuid4

from workbench_agent.application.errors import DomainError, not_found
from workbench_agent.application.idempotency import canonical, digest
from workbench_agent.application.repository_identity import snapshot_identity_hash
from workbench_agent.domain.evidence import safe_evidence_payload
from workbench_agent.domain.usage import ActualUsage, ReservationRequest
from workbench_agent.jobs.worker import Execution, Worker
from workbench_agent.runtime.budgets.service import BudgetService
from workbench_agent.persistence.errors import StaleExecution
from workbench_agent.execution.snapshot_reader import read_snapshot_lines


class SnapshotReader:
    def __init__(self, backend, execution):
        owner = getattr(execution, '_tool_owner', None)
        if not isinstance(execution, Execution) or not isinstance(owner, Worker) or owner.backend is not backend or not owner._owns_tool_execution(execution):
            raise ValueError('current Worker execution required')
        self.backend, self.execution = backend, execution
        self.budgets = BudgetService(backend)

    async def read(self, ref, path, start, end, *, invocation_id):
        execution, guard = self.execution, self.execution.guard
        context = execution.claim.context
        if not execution._tool_owner._owns_tool_execution(execution):
            raise DomainError('STALE_EXECUTION', 409)
        await execution.check_operation()
        permissions = execution.tool_permissions
        if permissions is None or not permissions.allows(context, 'repository.read', execution._expert_name):
            raise DomainError('FORBIDDEN', 403)
        objects = getattr(self.backend, '_snapshot_objects', None)
        if objects is None or not callable(getattr(objects, 'get', None)):
            raise DomainError('SNAPSHOT_UNAVAILABLE', 503)
        args = {'snapshot': ref.model_dump(mode='json'), 'path': path, 'start': start, 'end': end}
        async with self.backend.create_uow().begin_write() as uow:
            _, actual = await self.budgets._owned(uow, guard)
            if actual != context or ref not in context.repositories:
                raise not_found()
            scope = await self.backend.runs.current_scope(uow, guard.team_id, guard.run_id)
            if not scope:
                raise DomainError('QUERY_SCOPE_REQUIRED', 409)
            row = await self.backend.configuration.one(uow, 'repo_snapshots', team_id=context.team_id, agent_id=context.agent_id, snapshot_id=ref.resource_id, status='ready')
            if not row or row['commit_sha'] != ref.version or snapshot_identity_hash(row) != ref.content_hash:
                raise not_found()
            previous = await self.backend.tools.invocation(uow, guard.team_id, guard.run_id, invocation_id)
            if previous:
                if previous['args_hash'] != digest(args) or previous['tool_version'] != ref.resource_id:
                    raise DomainError('INVOCATION_CONFLICT', 409)
                if previous['status'] != 'succeeded' or not previous['receipt_ref']:
                    raise DomainError('INVOCATION_ALREADY_STARTED', 409)
                receipt = json.loads(previous['receipt_ref'])
                if receipt.get('kind') != 'snapshot-evidence-v1' or receipt.get('scope_id') != scope['scope_id'] or receipt.get('snapshot') != ref.model_dump(mode='json'):
                    raise DomainError('STALE_QUERY_SCOPE', 409)
                payload = await self.backend.tools.evidence_payload(uow, guard.team_id, guard.run_id, receipt['evidence_id'])
                if not payload or payload['truncated']:
                    raise DomainError('EVIDENCE_UNAVAILABLE', 409)
                return {'data': json.loads(payload['payload']), 'evidence_ref': receipt['evidence_id']}
            if await self.backend.tools.logical_invocation_count(uow, guard.team_id, guard.run_id) >= 4:
                raise DomainError('EXPERT_TOOL_LIMIT', 409)
            ident = str(uuid4())
            await self.budgets.reserve(guard, ReservationRequest(invocation_id=ident, kind='tool', tool_attempts=1), uow=uow)
            permit = await self.budgets.mark_sent(guard, ident, uow=uow)
            if not permit.dispatch_allowed:
                raise DomainError('INVOCATION_ALREADY_STARTED', 409)
            now = await self.backend.clock.now_ms(uow)
            await self.backend.tools.insert_invocation(uow, dict(team_id=guard.team_id, invocation_id=ident, run_id=guard.run_id, logical_call_id=invocation_id, tool_version=ref.resource_id, args_hash=digest(args), attempt=1, status='possibly_sent', receipt_ref=None, created_at=now, updated_at=now))
            object_ref, scope_id = row['storage_ref'], scope['scope_id']
        try:
            try:
                archive = await asyncio.to_thread(objects.get, object_ref)
                selected = await asyncio.to_thread(read_snapshot_lines, archive, path, start, end)
            except (OSError, ValueError) as error:
                raise DomainError('SNAPSHOT_UNAVAILABLE', 409) from error
        finally:
            try:
                async with self.backend.create_uow().begin_write() as uow:
                    await self.budgets.settle(guard, ident, ActualUsage(input_tokens=0, output_tokens=0), uow=uow)
            except (StaleExecution, DomainError) as error:
                if isinstance(error, DomainError) and error.code not in ('FORBIDDEN', 'NOT_FOUND'):
                    raise
                async with self.backend.create_uow().begin_write() as uow:
                    await self.budgets.quarantine(guard, ident, ActualUsage(input_tokens=0, output_tokens=0), uow=uow)
                raise
        await execution.check_operation()
        if not execution._tool_owner._owns_tool_execution(execution) or not permissions.allows(context, 'repository.read', execution._expert_name):
            raise DomainError('FORBIDDEN', 403)
        data = {'commit': ref.version, 'path': path, 'start': start, 'end': end, 'sha256': selected.sha256,
                'lines': [{'line': number, 'text': text} for number, text in selected.lines]}
        payload, truncated = safe_evidence_payload(data, 65536)
        async with self.backend.create_uow().begin_write() as uow:
            await self.budgets._owned(uow, guard)
            current = await self.backend.runs.current_scope(uow, guard.team_id, guard.run_id)
            if not current or current['scope_id'] != scope_id:
                raise DomainError('STALE_QUERY_SCOPE', 409)
            evidence_id, now = str(uuid4()), await self.backend.clock.now_ms(uow)
            await self.backend.tools.insert_evidence(uow, dict(team_id=guard.team_id, evidence_id=evidence_id, run_id=guard.run_id, scope_id=scope_id, source='repository.read', quality='truncated' if truncated else 'valid', time_window=canonical({}), artifact_ref=None, summary=digest(data), created_at=now, updated_at=now))
            await self.backend.tools.insert_evidence_payload(uow, dict(team_id=guard.team_id, evidence_id=evidence_id, run_id=guard.run_id, payload=payload, truncated=int(truncated), created_at=now))
            receipt = canonical({'kind': 'snapshot-evidence-v1', 'scope_id': scope_id, 'snapshot': ref.model_dump(mode='json'), 'evidence_id': evidence_id})
            if not await self.backend.tools.complete_invocation(uow, guard.team_id, guard.run_id, ident, 'succeeded', receipt, now):
                raise DomainError('INVOCATION_CONFLICT', 409)
        return {'data': json.loads(payload), 'evidence_ref': evidence_id}

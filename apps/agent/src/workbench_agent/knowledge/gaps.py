"""Guarded and team-authorized knowledge gap records."""
from workbench_agent.runtime.graphs.ids import GraphId
import json
from uuid import uuid4

from workbench_agent.application.authorization import Authorization
from workbench_agent.application.errors import DomainError, not_found
from workbench_agent.application.idempotency import Idempotency, canonical, digest
from workbench_agent.domain.guards import TERMINAL_RUN_STATUSES
from workbench_agent.domain.identity import AuthContext
from workbench_agent.knowledge.models import GapDraftTemplate
from workbench_agent.knowledge.service import KnowledgeService
from workbench_agent.observability import redacted_content

REASONS = frozenset({'kb.empty', 'kb.unanswerable', 'kb.invalid_citation', 'low_quality'})


class KnowledgeGaps:
    def __init__(self, backend):
        self.backend, self.store = backend, backend.knowledge
        self.authorization, self.idempotency = Authorization(backend), Idempotency(backend)

    async def _authorize(self, uow, auth, role):
        if auth.agent_scope is not None or auth.identity_kind == 'connector':
            raise not_found()
        await self.authorization.current(uow, auth, minimum=role)

    async def _visible(self, uow, auth, row, minimum='viewer'):
        if not row or row['problem_type'] not in REASONS:
            raise not_found()
        run = await self.backend.runs.one(uow, 'runs', team_id=auth.team_id, run_id=row['run_id'])
        if not run:
            raise not_found()
        await self.authorization.current(uow, auth, run['agent_id'], minimum)
        return run

    @staticmethod
    def _view(row):
        try:
            scope = json.loads(row['query_scope']) if row['query_scope'] else None
            trace = json.loads(row['source_trace'])
        except (ValueError, TypeError):
            scope, trace = None, {}
        return {key: row[key] for key in ('gap_id', 'run_id', 'problem_type', 'review_status', 'summary',
            'created_at', 'updated_at', 'gap_revision', 'resolved_entry_id', 'resolved_version',
            'replay_run_id', 'verification_status')} | {'query_scope': scope, 'note': trace.get('review_note') if isinstance(trace, dict) else None}

    async def record(self, execution, reason, *, expected_scope_id):
        if reason not in REASONS:
            raise DomainError('KB_INVALID_GAP_REASON', 422)
        context, guard = execution.claim.context, execution.guard
        if context.graph_id != GraphId.KNOWLEDGE_CITATION_REVIEW or not expected_scope_id:
            raise DomainError('FORBIDDEN', 403)
        auth = AuthContext(team_id=context.team_id, subject=context.subject, roles=context.roles,
                           identity_kind=context.identity_kind, agent_scope=context.agent_scope)
        async with self.backend.create_uow().begin_write() as uow:
            await self.backend.lease_guard.require(uow, guard, {'running'})
            await self.authorization.current(uow, auth, context.agent_id, 'operator')
            scope = await self.backend.runs.current_scope(uow, guard.team_id, guard.run_id)
            if not scope or scope['scope_id'] != expected_scope_id:
                raise DomainError('STALE_QUERY_SCOPE', 409)
            identity = digest({'run_id': guard.run_id, 'scope_id': scope['scope_id'], 'reason': reason, 'graph': GraphId.KNOWLEDGE_CITATION_REVIEW})
            existing = await self.store.gap_by_digest(uow, guard.team_id, identity)
            if existing:
                return self._view(existing)
            message = await self.backend.runs.one(uow, 'messages', team_id=guard.team_id, run_id=guard.run_id, role='user')
            if not message:
                raise not_found()
            try:
                question = json.loads(message['content_ref'])['input']['text']
                safe, truncated = redacted_content(question, 1024)
                summary = json.loads(safe) if safe and not truncated else '问题内容已省略'
                current_scope = json.loads(scope['scope_json'])
                visible_scope = {key: current_scope[key] for key in ('service', 'environment', 'region')}
            except (ValueError, KeyError, TypeError):
                raise DomainError('KB_INVALID_GAP', 422) from None
            now, gap_id = await self.backend.clock.now_ms(uow), str(uuid4())
            await self.store.insert_gap(uow, {'gap_id': gap_id, 'team_id': guard.team_id, 'run_id': guard.run_id,
                'source_trace': canonical({'scope_id': scope['scope_id'], 'graph': GraphId.KNOWLEDGE_CITATION_REVIEW}),
                'problem_type': reason, 'review_status': 'open', 'summary': summary,
                'query_scope': canonical(visible_scope), 'dedup_digest': identity, 'gap_revision': 0,
                'created_at': now, 'updated_at': now})
            return self._view(await self.store.gap(uow, guard.team_id, gap_id))

    async def record_feedback_in(self, uow, auth, run, *, feedback_id):
        """Create one low_quality gap inside the feedback UoW."""
        if run['status'] not in TERMINAL_RUN_STATUSES:
            return None
        from workbench_agent.domain.context import RunContext
        context = RunContext.model_validate_json(run['versions'])
        if context.graph_id != GraphId.KNOWLEDGE_CITATION_REVIEW:
            return None
        scope = await self.backend.runs.current_scope(uow, auth.team_id, run['run_id'])
        if not scope:
            return None
        identity = digest({
            'run_id': run['run_id'],
            'scope_id': scope['scope_id'],
            'reason': 'low_quality',
            'graph': GraphId.KNOWLEDGE_CITATION_REVIEW,
        })
        existing = await self.store.gap_by_digest(uow, auth.team_id, identity)
        if existing:
            return existing['gap_id']
        message = await self.backend.runs.one(
            uow, 'messages', team_id=auth.team_id, run_id=run['run_id'], role='user'
        )
        if not message:
            raise not_found()
        try:
            question = json.loads(message['content_ref'])['input']['text']
            safe, truncated = redacted_content(question, 1024)
            summary = json.loads(safe) if safe and not truncated else '问题内容已省略'
            current_scope = json.loads(scope['scope_json'])
            visible_scope = {
                key: current_scope[key]
                for key in ('service', 'environment', 'region')
            }
        except (ValueError, KeyError, TypeError):
            raise DomainError('KB_INVALID_GAP', 422) from None
        now, gap_id = await self.backend.clock.now_ms(uow), str(uuid4())
        await self.store.insert_gap(uow, {
            'gap_id': gap_id,
            'team_id': auth.team_id,
            'run_id': run['run_id'],
            'source_trace': canonical({
                'scope_id': scope['scope_id'],
                'graph': GraphId.KNOWLEDGE_CITATION_REVIEW,
                'feedback_id': feedback_id,
            }),
            'problem_type': 'low_quality',
            'review_status': 'open',
            'summary': summary,
            'query_scope': canonical(visible_scope),
            'dedup_digest': identity,
            'gap_revision': 0,
            'created_at': now,
            'updated_at': now,
        })
        return gap_id

    async def list(self, auth, *, limit=50, after=None, status='open'):
        async with self.backend.create_uow().begin_read() as uow:
            await self._authorize(uow, auth, 'maintainer')
            items, cursor = [], after
            while len(items) <= limit:
                rows = await self.store.gaps(uow, auth.team_id, limit=100, after=cursor, status=status)
                if not rows:
                    break
                for row in rows:
                    cursor = row['gap_id']
                    try:
                        await self._visible(uow, auth, row)
                    except DomainError as error:
                        if error.status in (403, 404):
                            continue
                        raise
                    items.append(self._view(row))
                    if len(items) > limit:
                        break
                if len(rows) < 100:
                    break
            return {'items': items[:limit], 'next_cursor': items[limit - 1]['gap_id'] if len(items) > limit else None}

    async def get(self, auth, gap_id):
        async with self.backend.create_uow().begin_read() as uow:
            await self._authorize(uow, auth, 'operator')
            row = await self.store.gap(uow, auth.team_id, gap_id)
            await self._visible(uow, auth, row, 'operator')
            return self._view(row)

    async def to_draft(self, auth, gap_id, *, expected_revision, key):
        async with self.backend.create_uow().begin_write() as uow:
            await self._authorize(uow, auth, 'maintainer')
            await self._visible(uow, auth, await self.store.gap(uow, auth.team_id, gap_id), 'maintainer')
            async def operation():
                row = await self.store.gap(uow, auth.team_id, gap_id)
                await self._visible(uow, auth, row, 'maintainer')
                if row['gap_revision'] != expected_revision:
                    raise DomainError('REVISION_CONFLICT', 409)
                if row['review_status'] != 'open' or row['resolved_entry_id']:
                    raise DomainError('KB_GAP_ALREADY_LINKED', 409)
                try:
                    scope = json.loads(row['query_scope'])
                    template = GapDraftTemplate(title=row['summary'][:200], gap_id=gap_id, run_id=row['run_id'],
                        **{key: scope.get(key) for key in ('service', 'environment', 'region')})
                except (ValueError, TypeError, AttributeError):
                    raise DomainError('KB_INVALID_GAP', 422) from None
                draft = await KnowledgeService(self.backend).create_gap_draft_in(uow, auth, template)
                now = await self.backend.clock.now_ms(uow)
                if not await self.store.cas_gap(uow, auth.team_id, gap_id, expected_revision,
                    {'resolved_entry_id': draft['entry_id'], 'gap_revision': expected_revision + 1, 'updated_at': now}):
                    raise DomainError('REVISION_CONFLICT', 409)
                return draft
            return await self.idempotency.execute_team(uow, auth, 'kb.gap.draft', key,
                {'gap_id': gap_id, 'revision': expected_revision}, operation, status=201)

    async def update(self, auth, gap_id, *, status, entry_id=None, version=None, verification_run_id=None,
                     note=None, expected_revision, key):
        if status not in ('resolved', 'dismissed') or not isinstance(note, str) or not note.strip() or len(note) > 2048:
            raise DomainError('KB_INVALID_GAP', 422)
        payload = {'gap_id': gap_id, 'status': status, 'entry_id': entry_id, 'version': version,
                   'verification_run_id': verification_run_id, 'note': note, 'revision': expected_revision}
        async with self.backend.create_uow().begin_write() as uow:
            await self._authorize(uow, auth, 'maintainer')
            await self._visible(uow, auth, await self.store.gap(uow, auth.team_id, gap_id), 'maintainer')
            async def operation():
                row = await self.store.gap(uow, auth.team_id, gap_id)
                await self._visible(uow, auth, row, 'maintainer')
                if row['gap_revision'] != expected_revision:
                    raise DomainError('REVISION_CONFLICT', 409)
                if row['review_status'] != 'open':
                    raise DomainError('KB_INVALID_GAP', 409)
                if status == 'resolved':
                    if not entry_id or not version or row['resolved_entry_id'] != entry_id or not verification_run_id or row['replay_run_id'] != verification_run_id:
                        raise DomainError('KB_INVALID_VERIFICATION', 422)
                    if not await self.store.version(uow, auth.team_id, entry_id, version):
                        raise not_found()
                    from workbench_agent.application.runs import RunService
                    verified = await RunService(self.backend).view(uow, auth, verification_run_id)
                    if verified.status not in TERMINAL_RUN_STATUSES or verified.outcome is None:
                        raise DomainError('KB_INVALID_VERIFICATION', 409)
                    answer = verified.outcome.get('knowledge_answer') or {}
                    cited = any(isinstance(citation, dict) and isinstance(citation.get('source_id'), str)
                        and citation['source_id'].startswith(f'kb:{entry_id}@{version}#')
                        for claim in answer.get('claims', []) if isinstance(claim, dict)
                        for citation in claim.get('citations', []))
                    if answer.get('verification') != 'passed' or not cited:
                        raise DomainError('KB_INVALID_VERIFICATION', 409)
                trace = json.loads(row['source_trace'])
                trace['review_note'] = note.strip()
                now = await self.backend.clock.now_ms(uow)
                changes = {'review_status': status, 'source_trace': canonical(trace), 'updated_at': now,
                    'gap_revision': expected_revision + 1, 'verification_status': 'human_confirmed' if status == 'resolved' else None}
                if status == 'resolved':
                    changes['resolved_version'] = version
                if not await self.store.cas_gap(uow, auth.team_id, gap_id, expected_revision, changes):
                    raise DomainError('REVISION_CONFLICT', 409)
                await KnowledgeService(self.backend)._audit(uow, auth, 'kb.gap.' + status, gap_id,
                                                           note=note.strip(), linked_entry_id=entry_id, version=version)
                return self._view(await self.store.gap(uow, auth.team_id, gap_id))
            return await self.idempotency.execute_team(uow, auth, 'kb.gap.update', key, payload, operation)

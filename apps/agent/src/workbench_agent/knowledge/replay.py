"""Admit a fresh Run from authorized original input, atomically linking its gap."""
import json
from uuid import uuid4

from workbench_agent.application.errors import DomainError, not_found
from workbench_agent.application.idempotency import Idempotency
from workbench_agent.domain.guards import TERMINAL_RUN_STATUSES
from workbench_agent.domain.runs import CreateRun
from workbench_agent.knowledge.gaps import KnowledgeGaps


class KBReplayService:
    def __init__(self, backend, run_service):
        self.backend, self.run_service = backend, run_service
        self.store = backend.knowledge
        self.gaps, self.idempotency = KnowledgeGaps(backend), Idempotency(backend)

    async def replay(self, auth, gap_id, *, expected_revision, key):
        async with self.backend.create_uow().begin_write() as uow:
            await self.gaps._authorize(uow, auth, 'operator')
            gap = await self.store.gap(uow, auth.team_id, gap_id)
            original = await self.gaps._visible(uow, auth, gap, 'operator')
            async def operation():
                if gap['gap_revision'] != expected_revision:
                    raise DomainError('REVISION_CONFLICT', 409)
                if gap['review_status'] != 'open' or original['status'] not in TERMINAL_RUN_STATUSES:
                    raise DomainError('KB_INVALID_GAP', 409)
                message = await self.backend.runs.one(uow, 'messages', team_id=auth.team_id,
                    run_id=gap['run_id'], role='user')
                scope = await self.backend.runs.current_scope(uow, auth.team_id, gap['run_id'])
                if not message or not scope:
                    raise not_found()
                try:
                    original_payload = json.loads(message['content_ref'])
                    current_scope = json.loads(scope['scope_json'])
                    stored_scope = json.loads(gap['query_scope'])
                    if any(current_scope.get(name) != stored_scope.get(name) for name in ('service', 'environment', 'region')):
                        raise DomainError('KB_SCOPE_CHANGED', 409)
                    original_payload.update(thread_id=None, client_message_id=None, expected_revision=None)
                    original_payload['business_context'] = {**original_payload.get('business_context', {}),
                                                            'query_scope': current_scope}
                    request = CreateRun.model_validate(original_payload)
                except (ValueError, KeyError, TypeError):
                    raise DomainError('KB_REPLAY_INPUT_UNAVAILABLE', 409) from None
                # The outer team receipt is the stable replay intent. A fresh internal
                # admission key cannot collide with a public run.create key reused by
                # the caller; both receipts commit or roll back in this same UoW.
                accepted = await self.run_service.create_in(uow, auth, request, 'kb-replay:' + uuid4().hex)
                if accepted.run_id == gap['run_id']:
                    raise DomainError('KB_INVALID_VERIFICATION', 409)
                now = await self.backend.clock.now_ms(uow)
                if not await self.store.cas_gap(uow, auth.team_id, gap_id, expected_revision,
                    {'replay_run_id': accepted.run_id, 'verification_status': 'pending',
                     'gap_revision': expected_revision + 1, 'updated_at': now}):
                    raise DomainError('REVISION_CONFLICT', 409)
                from workbench_agent.knowledge.service import KnowledgeService
                await KnowledgeService(self.backend)._audit(uow, auth, 'kb.gap.replay.link', gap_id,
                    run_id=accepted.run_id, original_run_id=gap['run_id'])
                links = {**accepted.links, 'original_run': f"/v1/runs/{gap['run_id']}", 'knowledge_gap': f'/v1/kb/gaps/{gap_id}'}
                return accepted.model_copy(update={'links': links}).model_dump(mode='json')
            return await self.idempotency.execute_team(uow, auth, 'kb.gap.replay', key,
                {'gap_id': gap_id, 'revision': expected_revision}, operation, status=202)

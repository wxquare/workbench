"""Record user feedback independently of evaluation and external observability."""

import json
from uuid import uuid4
from workbench_agent.application.authorization import Authorization
from workbench_agent.application.errors import DomainError, not_found
from workbench_agent.application.idempotency import Idempotency, canonical


class FeedbackService:
    def __init__(self, backend):
        self.backend = backend
        self.authorization = Authorization(backend)
        self.receipts = Idempotency(backend)

    async def record(self, auth, run_id, rating, comment, key, *, reason=None):
        if reason not in {None, 'irrelevant', 'citation_error', 'stale', 'other'}:
            raise DomainError('INVALID_FEEDBACK_REASON', 422)
        if rating == 1 and reason is not None:
            raise DomainError('INVALID_FEEDBACK_REASON', 422)
        async with self.backend.create_uow().begin_write() as uow:
            run = await self.backend.runs.one(uow, 'runs', team_id=auth.team_id, run_id=run_id)
            if not run:
                raise not_found()
            await self.authorization.current(uow, auth, run['agent_id'])
            if auth.identity_kind == 'connector':
                raise DomainError('FORBIDDEN', 403)
            payload = {'run_id': run_id, 'rating': rating, 'comment': comment}
            if reason is not None:
                payload['reason'] = reason
                detail = {
                    'subject': auth.subject,
                    'rating': rating,
                    'comment': comment,
                    'reason': reason,
                }
            else:
                detail = {
                    'subject': auth.subject,
                    'rating': rating,
                    'comment': comment,
                }

            async def append():
                now = await self.backend.clock.now_ms(uow)
                feedback_id = str(uuid4())
                await self.backend.feedback.append(
                    uow,
                    {
                        'feedback_id': feedback_id,
                        'team_id': auth.team_id,
                        'run_id': run_id,
                        'source_trace': canonical({'run_id': run_id}),
                        'problem_type': 'user_feedback',
                        'review_status': 'pending',
                        'payload': canonical(detail),
                        'created_at': now,
                        'updated_at': now,
                    },
                )
                gap_id = None
                if rating == -1:
                    from workbench_agent.knowledge.gaps import KnowledgeGaps

                    gap_id = await KnowledgeGaps(self.backend).record_feedback_in(
                        uow, auth, run, feedback_id=feedback_id
                    )
                return {
                    'feedback_id': feedback_id,
                    'run_id': run_id,
                    'rating': rating,
                    'comment': comment,
                    'reason': reason,
                    'knowledge_gap_id': gap_id,
                    'review_status': 'pending',
                }

            return await self.receipts.execute(
                uow,
                auth,
                run['agent_id'],
                f'/v1/runs/{run_id}/feedback',
                key,
                payload,
                append,
                status=201,
            )

    async def list(self, auth, run_id, limit, after):
        async with self.backend.create_uow().begin_read() as uow:
            run = await self.backend.runs.one(uow, 'runs', team_id=auth.team_id, run_id=run_id)
            if not run:
                raise not_found()
            await self.authorization.current(uow, auth, run['agent_id'])
            if auth.identity_kind == 'connector':
                raise DomainError('FORBIDDEN', 403)
            rows = await self.backend.feedback.list_for_run(uow, auth.team_id, run_id, limit, after)
            items = []
            for row in rows[:limit]:
                payload = json.loads(row['payload'])
                items.append(
                    {
                        'feedback_id': row['feedback_id'],
                        'run_id': run_id,
                        'rating': payload.get('rating'),
                        'comment': payload.get('comment'),
                        'reason': payload.get('reason'),
                        'review_status': row['review_status'],
                    }
                )
            return {
                'items': items,
                'next_cursor': items[-1]['feedback_id'] if len(rows) > limit else None,
            }

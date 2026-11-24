"""Authorized review inbox projection without a second decision state machine."""
import base64
import json
import time

from workbench_agent.application.errors import DomainError
from workbench_agent.application.review_facts import action_review_facts, run_review_facts
from workbench_agent.application.review_eligibility import review_eligibility


REVIEW_TYPES = (
    'run_input',
    'run_approval',
    'action_approval',
    'action_reapproval',
    'action_reconcile',
)
MAX_CANDIDATES = 5_000
CURSOR_VERSION = 1


def _encode_cursor(value):
    raw = json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True)
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip('=')


def _decode_cursor(value, *, auth, state, kind):
    try:
        raw = value + '=' * (-len(value) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(raw))
        expected = {
            'version': CURSOR_VERSION,
            'team_id': auth.team_id,
            'subject': auth.subject,
            'state': state,
            'type': kind,
        }
        if not isinstance(decoded, dict) or any(
            decoded.get(key) != value for key, value in expected.items()
        ):
            raise ValueError()
        prefix = decoded['prefix']
        if not isinstance(prefix, list) or len(prefix) != 4:
            raise ValueError()
        sort_key = decoded['after']
        if not isinstance(sort_key, list) or len(sort_key) != 4:
            raise ValueError()
        return tuple(sort_key)
    except Exception:
        raise DomainError('INVALID_CURSOR', 422) from None


def _sort_key(item):
    return (
        1 if item['due_at_ms'] is None else 0,
        item['due_at_ms'] or 0,
        item['created_at_ms'],
        item['item_id'],
    )


def _public_item(item):
    return {
        key: value
        for key, value in item.items()
        if key not in ('created_at_ms', 'due_at_ms', 'interrupt', 'action', 'approval')
    }


class ReviewService:
    def __init__(self, backend):
        self.backend = backend

    async def _items(self, auth, state):
        async with self.backend.create_uow().begin_read() as uow:
            source = await self.backend.reviews.candidates(uow, auth, state)
            if len(source['runs']) > MAX_CANDIDATES or len(source['actions']) > MAX_CANDIDATES:
                raise DomainError('REVIEWS_CAPACITY_EXCEEDED', 503)
            now = await self.backend.clock.now_ms(uow)
            member = await self.backend.configuration.one(
                uow, 'team_members', team_id=auth.team_id,
                subject=auth.subject, status='active',
            )
            team = await self.backend.configuration.one(
                uow, 'teams', team_id=auth.team_id, status='active',
            )
            if member is None or team is None:
                raise DomainError('FORBIDDEN', 403)
            prefix = (auth.team_id, auth.subject, member['role'], team['revision'])
            result = []
            for run in source['runs']:
                interrupt = source['interrupts'].get(run['run_id'])
                if interrupt is None or run['run_id'] not in source['checkpoint_run_ids']:
                    continue
                checkpoint = await self.backend.runs.checkpoint(
                    uow, auth.team_id, run['run_id']
                )
                facts = await run_review_facts(
                    self.backend, uow, auth, run, interrupt, checkpoint
                )
                kind = (
                    'run_approval'
                    if json.loads(interrupt['schema']).get('kind') == 'approval'
                    else 'run_input'
                )
                eligibility = review_eligibility(
                    kind, facts=facts, now_ms=facts.get('now_ms', now)
                )
                expired = eligibility.expired
                if not eligibility.visible or (
                    state == 'pending' and expired
                ) or (state == 'expired' and not expired):
                    continue
                due = facts.get('deadline_at') if eligibility.visible else None
                result.append({
                    'item_id': f"{kind}:{interrupt['interrupt_id']}",
                    'type': kind,
                    'agent_id': run['agent_id'],
                    'run_id': run['run_id'],
                    'action_id': None,
                    'title': '审批任务' if kind == 'run_approval' else '补充任务输入',
                    'due_at': self._timestamp(due),
                    'created_at': self._timestamp(interrupt['created_at']),
                    'expired': expired,
                    'available_operations': list(eligibility.operations),
                    'simulated': False,
                    'source_label': 'run',
                    'links': {'run': f'/v1/runs/{run["run_id"]}'},
                    'created_at_ms': interrupt['created_at'],
                    'due_at_ms': due,
                    'interrupt': interrupt,
                })

            for action in source['actions']:
                approval = source['approvals'].get(action['action_id'])
                outbox = source['outboxes'].get(action['action_id'])
                facts = await action_review_facts(
                    self.backend, uow, auth, action, outbox
                )
                kinds = []
                if action['state'] in ('proposed', 'reapproval_pending'):
                    kinds.append('action_approval')
                if action['state'] == 'failed':
                    kinds.append('action_reapproval')
                if action['state'] == 'unknown':
                    kinds.append('action_reconcile')
                for kind in kinds:
                    eligibility = review_eligibility(
                        kind, facts=facts, now_ms=facts.get('now_ms', now)
                    )
                    if not eligibility.visible or eligibility.expired:
                        continue
                    due = approval['expires_at'] if approval else None
                    result.append({
                        'item_id': f"{kind}:{action['action_id']}",
                        'type': kind,
                        'agent_id': source['run_by_id'].get(action['run_id'], {}).get('agent_id'),
                        'run_id': action['run_id'],
                        'action_id': action['action_id'],
                        'title': {
                            'action_approval': '审批外部动作',
                            'action_reapproval': '处理失败动作',
                            'action_reconcile': '核对投递结果',
                        }[kind],
                        'due_at': self._timestamp(due),
                        'created_at': self._timestamp(action['created_at']),
                        'expired': False,
                        'available_operations': list(eligibility.operations),
                        'simulated': False,
                        'source_label': 'action',
                        'links': {'run': f'/v1/runs/{action["run_id"]}'},
                        'created_at_ms': action['created_at'],
                        'due_at_ms': due,
                        'action': action,
                        'approval': approval,
                    })
            return result, prefix

    @staticmethod
    def _timestamp(value):
        if value is None:
            return None
        from datetime import datetime, timezone
        return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat().replace(
            '+00:00', 'Z'
        )

    async def list(self, auth, *, state='pending', kind=None, limit=20, after=None):
        if auth.identity_kind == 'connector':
            raise DomainError('FORBIDDEN', 403)
        if kind is not None and kind not in REVIEW_TYPES:
            raise DomainError('INVALID_REQUEST', 422)
        items, prefix = await self._items(auth, state)
        if kind is not None:
            items = [item for item in items if item['type'] == kind]
        items.sort(key=_sort_key)
        if after is not None:
            cursor = _decode_cursor(after, auth=auth, state=state, kind=kind)
            if tuple(cursor[:4]) != prefix:
                raise DomainError('INVALID_CURSOR', 422)
            cursor = tuple(cursor[4:])
            items = [item for item in items if _sort_key(item) > cursor]
        page = items[:limit]
        next_cursor = (
            _encode_cursor({
                'version': CURSOR_VERSION,
                'team_id': auth.team_id,
                'subject': auth.subject,
                'state': state,
                'type': kind,
                'prefix': list(prefix),
                'after': list(_sort_key(page[-1])),
            })
            if len(items) > limit
            else None
        )
        return {
            'items': [_public_item(item) for item in page],
            'next_cursor': next_cursor,
            'as_of': self._timestamp(time.time_ns() // 1_000_000),
        }

    async def summary(self, auth):
        if auth.identity_kind == 'connector':
            raise DomainError('FORBIDDEN', 403)
        items, _ = await self._items(auth, 'pending')
        counts = {kind: 0 for kind in REVIEW_TYPES}
        for item in items:
            counts[item['type']] += 1
        return {
            'total': len(items),
            'by_type': counts,
            'as_of': self._timestamp(time.time_ns() // 1_000_000),
        }

    async def get(self, auth, item_id):
        if auth.identity_kind == 'connector':
            raise DomainError('FORBIDDEN', 403)
        kind, separator, object_id = item_id.partition(':')
        if not separator or kind not in REVIEW_TYPES or not object_id:
            raise DomainError('INVALID_REQUEST', 422)
        items, _ = await self._items(auth, 'pending')
        match = next((item for item in items if item['item_id'] == item_id), None)
        if match is None:
            items, _ = await self._items(auth, 'expired')
            match = next((item for item in items if item['item_id'] == item_id), None)
        if match is None:
            from workbench_agent.application.errors import not_found
            raise not_found()
        detail = {}
        if match.get('interrupt'):
            detail = {
                'schema': json.loads(match['interrupt']['schema']),
                'expires_at': self._timestamp(match['interrupt']['expires_at']),
            }
        if match.get('action'):
            payload = json.loads(match['action']['payload_ref'])
            detail = {
                'arguments': payload['arguments'],
                'target': payload['target'],
                'binding': payload['binding'],
                'payload_hash': match['action']['payload_hash'],
            }
        return {'item': _public_item(match), 'detail': detail}

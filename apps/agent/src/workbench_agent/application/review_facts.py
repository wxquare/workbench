"""Assemble review eligibility facts from existing guarded state."""

import json

from workbench_agent.application.authorization import Authorization
from workbench_agent.application.action_authority import require_action_authority
from workbench_agent.application.errors import DomainError
from workbench_agent.domain.context import RunContext
from workbench_agent.domain.identity import AuthContext


def _denied():
    return {'same_team': False, 'operator': False}


async def _authorized(uow, backend, auth, agent_id):
    try:
        await Authorization(backend).current(uow, auth, agent_id, 'operator')
    except DomainError:
        return None
    return auth


async def run_review_facts(backend, uow, auth, run, interrupt, checkpoint):
    if interrupt is None or checkpoint is None:
        return _denied()
    try:
        current = await _authorized(uow, backend, auth, run['agent_id'])
        descriptor = json.loads(interrupt['schema'])
        real = next(
            (
                item
                for item in checkpoint['interrupts']
                if item.id == interrupt['graph_interrupt_id']
            ),
            None,
        )
        now = await backend.clock.now_ms(uow)
        approval_matches = True
        if descriptor.get('kind') == 'approval':
            approval_matches = (
                real is not None and checkpoint['values'].get('args_hash') == interrupt['args_hash']
            )
        return {
            'same_team': current is not None,
            'operator': current is not None,
            'identity_kind': auth.identity_kind,
            'actor_matches': descriptor.get('actor') == auth.subject,
            'interrupt_open': (
                interrupt['status'] == 'open'
                and interrupt.get('accepted_resume_request_id') is None
            ),
            'waiting_matches': run['status'] in ('waiting_input', 'waiting_approval'),
            'approval_matches_checkpoint': approval_matches
            and real is not None
            and real.value == descriptor,
            'deadline_at': min(
                interrupt['expires_at'],
                run['waiting_deadline_at'] or interrupt['expires_at'],
                run['wall_deadline_at'],
            ),
            'now_ms': now,
        }
    except Exception:
        return _denied()


async def action_review_facts(backend, uow, auth, action, outbox):
    approval = await backend.actions.current_approval(uow, auth.team_id, action['action_id'])
    run = await backend.runs.one(uow, 'runs', team_id=auth.team_id, run_id=action['run_id'])
    if approval is None or run is None:
        return _denied()
    try:
        current = await _authorized(uow, backend, auth, run['agent_id'])
        if current is None:
            return _denied()
        authority = True
        try:
            await require_action_authority(backend, uow, action, approval, run, reader=auth)
        except DomainError:
            authority = False
        now = await backend.clock.now_ms(uow)
        return {
            'same_team': True,
            'operator': True,
            'identity_kind': auth.identity_kind,
            'subject_matches': approval['subject'] == auth.subject,
            'approval_live': (approval['decision'] == 'pending' and now < approval['expires_at']),
            'action_state_matches': action['state'] in ('proposed', 'reapproval_pending'),
            'authority_matches': authority,
            'payload_matches': (approval['args_hash'] == action['payload_hash']),
            'scope_matches': (await backend.runs.current_scope(uow, auth.team_id, run['run_id']))[
                'scope_id'
            ]
            == action['scope'],
            'action_failed': action['state'] == 'failed',
            'outbox_dead': bool(outbox) and outbox['status'] == 'dead',
            'receipt_absent': bool(outbox) and outbox.get('receipt_ref') is None,
            'previous_approval_approved': approval['decision'] == 'approve',
            'previous_approval_expired': now >= approval['expires_at'],
            'attempt': outbox.get('attempt', 0) if outbox else 0,
            'action_unknown': action['state'] == 'unknown',
            'outbox_unknown': bool(outbox) and outbox['status'] == 'unknown',
            'payload_ref_matches': bool(outbox)
            and (outbox['payload_ref'] == action['payload_ref']),
            'destination_matches': bool(outbox)
            and (outbox['destination'] in (getattr(backend, '_delivery_senders', None) or {})),
            'lookup_available': bool(outbox)
            and callable(
                getattr(
                    (getattr(backend, '_delivery_senders', None) or {}).get(outbox['destination']),
                    'lookup',
                    None,
                )
            ),
            'now_ms': now,
        }
    except Exception:
        return _denied()

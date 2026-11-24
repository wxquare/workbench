"""Side-effect-free eligibility projection for the review inbox."""
from dataclasses import dataclass
from typing import Literal


ReviewType = Literal[
    'run_input',
    'run_approval',
    'action_approval',
    'action_reapproval',
    'action_reconcile',
]
ReviewOperation = Literal[
    'resume',
    'approve',
    'reject',
    'retry',
    'reapproval',
    'reconcile',
]


@dataclass(frozen=True)
class ReviewEligibility:
    visible: bool
    expired: bool
    operations: tuple[ReviewOperation, ...]


def _all(facts, *names):
    return all(facts.get(name) is True for name in names)


def _common(facts):
    return (
        facts.get('same_team') is True
        and facts.get('operator') is True
        and facts.get('identity_kind') != 'connector'
    )


def review_eligibility(kind: ReviewType, *, facts: dict, now_ms: int) -> ReviewEligibility:
    if not _common(facts):
        return ReviewEligibility(False, False, ())

    if kind in ('run_input', 'run_approval'):
        visible = _all(
            facts,
            'actor_matches',
            'interrupt_open',
            'waiting_matches',
            'approval_matches_checkpoint',
        )
        if not visible:
            return ReviewEligibility(False, False, ())
        expired = int(facts.get('deadline_at', 0)) <= now_ms
        return ReviewEligibility(
            True,
            expired,
            () if expired else ('resume',),
        )

    if kind == 'action_approval':
        visible = _all(
            facts,
            'subject_matches',
            'approval_live',
            'action_state_matches',
            'authority_matches',
            'payload_matches',
            'scope_matches',
        )
        return ReviewEligibility(
            visible,
            False,
            ('approve', 'reject') if visible else (),
        )

    if kind == 'action_reapproval':
        visible = _all(
            facts,
            'subject_matches',
            'action_failed',
            'outbox_dead',
            'receipt_absent',
            'previous_approval_approved',
            'authority_matches',
        )
        if not visible:
            return ReviewEligibility(False, False, ())
        if facts.get('previous_approval_expired') is True:
            operations = ('reapproval',)
        elif int(facts.get('attempt', 0)) >= 8:
            operations = ('retry',)
        else:
            operations = ()
        return ReviewEligibility(True, False, operations)

    if kind == 'action_reconcile':
        visible = _all(
            facts,
            'action_unknown',
            'outbox_unknown',
            'authority_matches',
            'payload_ref_matches',
            'destination_matches',
            'lookup_available',
        )
        return ReviewEligibility(
            visible,
            False,
            ('reconcile',) if visible else (),
        )

    return ReviewEligibility(False, False, ())

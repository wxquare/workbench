from workbench_agent.application.review_eligibility import review_eligibility


NOW = 10_000


def test_valid_pending_run_input_is_actionable():
    result = review_eligibility(
        'run_input',
        facts={
            'same_team': True,
            'operator': True,
            'actor_matches': True,
            'interrupt_open': True,
            'waiting_matches': True,
            'approval_matches_checkpoint': True,
            'deadline_at': NOW + 1,
        },
        now_ms=NOW,
    )

    assert result.visible is True
    assert result.expired is False
    assert result.operations == ('resume',)


def test_run_input_requires_the_current_actor_and_an_open_interrupt():
    result = review_eligibility(
        'run_input',
        facts={
            'same_team': True,
            'operator': True,
            'actor_matches': False,
            'interrupt_open': False,
            'waiting_matches': True,
            'approval_matches_checkpoint': True,
            'deadline_at': NOW + 1,
        },
        now_ms=NOW,
    )

    assert result.visible is False
    assert result.operations == ()


def test_expired_run_input_is_read_only():
    result = review_eligibility(
        'run_input',
        facts={
            'same_team': True,
            'operator': True,
            'actor_matches': True,
            'interrupt_open': True,
            'waiting_matches': True,
            'approval_matches_checkpoint': True,
            'deadline_at': NOW,
        },
        now_ms=NOW,
    )

    assert result.visible is True
    assert result.expired is True
    assert result.operations == ()


def test_action_approval_requires_operator_and_designated_live_approval():
    valid = review_eligibility(
        'action_approval',
        facts={
            'same_team': True,
            'operator': True,
            'subject_matches': True,
            'approval_live': True,
            'action_state_matches': True,
            'authority_matches': True,
            'payload_matches': True,
            'scope_matches': True,
        },
        now_ms=NOW,
    )
    wrong_subject = review_eligibility(
        'action_approval',
        facts={
            'same_team': True,
            'operator': True,
            'subject_matches': False,
            'approval_live': True,
            'action_state_matches': True,
            'authority_matches': True,
            'payload_matches': True,
            'scope_matches': True,
        },
        now_ms=NOW,
    )

    assert valid.visible is True and valid.operations == ('approve', 'reject')
    assert wrong_subject.visible is False and wrong_subject.operations == ()


def test_action_reapproval_is_not_expired_with_the_old_approval():
    result = review_eligibility(
        'action_reapproval',
        facts={
            'same_team': True,
            'operator': True,
            'subject_matches': True,
            'action_failed': True,
            'outbox_dead': True,
            'receipt_absent': True,
            'previous_approval_approved': True,
            'previous_approval_expired': True,
            'attempt': 8,
            'authority_matches': True,
        },
        now_ms=NOW,
    )

    assert result.visible is True
    assert result.expired is False
    assert result.operations == ('reapproval',)


def test_unknown_action_offers_reconcile_only():
    result = review_eligibility(
        'action_reconcile',
        facts={
            'same_team': True,
            'operator': True,
            'action_unknown': True,
            'outbox_unknown': True,
            'authority_matches': True,
            'payload_ref_matches': True,
            'destination_matches': True,
            'lookup_available': True,
        },
        now_ms=NOW,
    )

    assert result.visible is True
    assert result.operations == ('reconcile',)


def test_connector_and_stale_snapshot_are_not_visible():
    result = review_eligibility(
        'action_reconcile',
        facts={
            'same_team': True,
            'operator': False,
            'identity_kind': 'connector',
            'action_unknown': True,
            'outbox_unknown': True,
            'authority_matches': True,
            'payload_ref_matches': True,
            'destination_matches': True,
            'lookup_available': True,
        },
        now_ms=NOW,
    )

    assert result.visible is False
    assert result.operations == ()

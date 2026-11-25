from workbench_agent.runtime.graphs.validation import EvidenceValidator


def test_validator_requires_completed_steps_and_current_valid_references():
    validator = EvidenceValidator()
    evidence = {
        'a': {
            'evidence_id': 'a',
            'scope_id': 'scope',
            'quality': 'valid',
            'source': 'logs',
            'reference_accessible': True,
        },
        'b': {
            'evidence_id': 'b',
            'scope_id': 'old',
            'quality': 'valid',
            'source': 'knowledge',
            'reference_accessible': True,
        },
        'c': {
            'evidence_id': 'c',
            'scope_id': 'scope',
            'quality': 'truncated',
            'source': 'logs',
            'reference_accessible': True,
        },
    }
    result = validator.validate(
        'scope',
        evidence,
        {'baseline', 'investigate'},
        completed_steps={'baseline'},
        referenced_ids={'a', 'b', 'c', 'missing'},
    )
    assert not result.valid
    assert result.valid_refs == ('a',)
    assert set(result.invalid_refs) == {'b', 'c', 'missing'}
    assert result.missing_steps == ('investigate',)


def test_validator_cannot_succeed_without_scope_or_references():
    result = EvidenceValidator().validate(
        None, {}, {'investigate'}, completed_steps={'investigate'}, referenced_ids=set()
    )
    assert not result.valid
    assert result.missing_scope
    assert result.missing_evidence


def test_unrecoverable_evidence_is_not_valid():
    evidence = {
        'a': {
            'evidence_id': 'a',
            'scope_id': 'scope',
            'quality': 'valid',
            'source': 'logs',
            'reference_accessible': False,
        }
    }
    result = EvidenceValidator().validate(
        'scope', evidence, {'investigate'}, completed_steps={'investigate'}, referenced_ids={'a'}
    )
    assert not result.valid and result.invalid_refs == ('a',)

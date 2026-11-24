"""Untrusted synthesis is limited to literal, authorized source excerpts."""

from workbench_agent.runtime.graphs.synthesis import (
    EvidenceExcerpt,
    ExcerptSelection,
    validate_selection,
)


def test_selection_requires_exact_text_and_authorized_reference():
    sources = {'known': ('First fact. Second fact.',), 'other': ('Another source.',)}
    valid = validate_selection(
        ExcerptSelection(excerpts=[EvidenceExcerpt(evidence_ref='known', quote='First fact.')]),
        sources,
    )
    assert valid == [('known', 'First fact.')]
    for ref, quote in [
        ('known', 'Invented fact.'),
        ('unknown', 'First fact.'),
        ('known', ' '),
        ('known', 'First fact. Second fact. trailing'),
    ]:
        assert (
            validate_selection(
                ExcerptSelection(excerpts=[EvidenceExcerpt(evidence_ref=ref, quote=quote)]), sources
            )
            is None
        )


def test_selection_rejects_empty_duplicate_and_excess():
    assert validate_selection(ExcerptSelection(excerpts=[]), {'one': ('hello',)}) is None
    item = EvidenceExcerpt(evidence_ref='one', quote='hello')
    assert validate_selection(ExcerptSelection(excerpts=[item, item]), {'one': ('hello',)}) is None

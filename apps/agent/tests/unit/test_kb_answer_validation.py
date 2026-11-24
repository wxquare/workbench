from uuid import uuid4
import pytest

from workbench_agent.domain.knowledge_answers import AnswerDraft
from workbench_agent.knowledge.answer_validation import (
    CitationValidationError,
    SourcePassage,
    validate_answer,
)


def test_claim_rejects_quote_not_found_in_exact_source():
    ref = str(uuid4())
    source_id = 'kb:entry@1#chunk'
    source = SourcePassage(
        evidence_ref=ref,
        source_id=source_id,
        content='检查库存后再重试。',
        title='库存',
        source_locator='检查，行1',
        source_uri=None,
    )
    draft = AnswerDraft.model_validate(
        {
            'answerable': True,
            'claims': [
                {
                    'text': '应删除库存数据。',
                    'citations': [
                        {'evidence_ref': ref, 'source_id': source_id, 'quote': '删除库存数据'}
                    ],
                }
            ],
        }
    )
    with pytest.raises(CitationValidationError):
        validate_answer(draft, {(ref, source_id): source})


def test_valid_citation_uses_trusted_metadata_and_preserves_exact_quote():
    ref = str(uuid4())
    source_id = 'kb:entry@1#chunk'
    source = SourcePassage(
        evidence_ref=ref,
        source_id=source_id,
        content='检查库存后再重试。',
        title='库存指南',
        source_locator='检查 · 行 1',
        source_uri='/app/knowledge/entry/versions/1#chunk',
    )
    draft = AnswerDraft.model_validate(
        {
            'answerable': True,
            'claims': [
                {
                    'text': '先检查库存。',
                    'citations': [
                        {'evidence_ref': ref, 'source_id': source_id, 'quote': '检查库存'}
                    ],
                }
            ],
        }
    )
    answer = validate_answer(draft, {(ref, source_id): source})
    citation = answer.claims[0].citations[0]
    assert citation.quote == '检查库存'
    assert citation.title == '库存指南'
    assert citation.source_uri == '/app/knowledge/entry/versions/1#chunk'


def test_citations_are_bound_to_evidence_and_version_together():
    ref, other = str(uuid4()), str(uuid4())
    source = SourcePassage(
        evidence_ref=ref,
        source_id='kb:entry@2#chunk',
        content='检查库存。',
        title='库存',
        source_locator='正文',
        source_uri=None,
    )
    draft = AnswerDraft.model_validate(
        {
            'answerable': True,
            'claims': [
                {
                    'text': '先检查。',
                    'citations': [
                        {
                            'evidence_ref': other,
                            'source_id': 'kb:entry@2#chunk',
                            'quote': '检查库存',
                        }
                    ],
                }
            ],
        }
    )
    with pytest.raises(CitationValidationError):
        validate_answer(draft, {(ref, source.source_id): source})

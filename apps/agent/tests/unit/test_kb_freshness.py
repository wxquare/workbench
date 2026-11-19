from datetime import datetime, timezone
from uuid import uuid4

import pytest

from workbench_agent.knowledge.answer_validation import collect_passages, validate_freshness, CitationValidationError
from workbench_agent.domain.knowledge_answers import AnswerDraft


def test_freshness_requires_valid_current_timestamps_for_every_citation():
    ref = str(uuid4())
    payload = {'schema_version': 'dod-tool-result-v1', 'status': 'ok', 'data': {'matches': [
        {'source_id': 'one', 'title': 'One', 'content': 'exact quote', 'updated_at': '2026-01-01T00:00:00Z'}]}}
    answer = AnswerDraft.model_validate({'answerable': True, 'claims': [{'text': 'fact', 'citations': [
        {'evidence_ref': ref, 'source_id': 'one', 'quote': 'exact quote'}]}], 'missing': []})
    sources = collect_passages({ref: payload})
    now = datetime(2026, 1, 1, 0, 30, tzinfo=timezone.utc)
    validate_freshness(answer, sources, now=now, max_age_seconds=3600)
    for age, check_time in [(None, now), (60, now), (3600, datetime(2025, 1, 1, tzinfo=timezone.utc))]:
        with pytest.raises(CitationValidationError):
            validate_freshness(answer, sources, now=check_time, max_age_seconds=age)
    payload['data']['matches'][0]['updated_at'] = None
    with pytest.raises(CitationValidationError):
        validate_freshness(answer, collect_passages({ref: payload}), now=now, max_age_seconds=3600)

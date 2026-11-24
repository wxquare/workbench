from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from workbench_agent.domain.outcomes import RunOutcome, UsageSummary
from workbench_agent.domain.knowledge_answers import KnowledgeAnswer
from workbench_agent.runtime.graphs.evidence_authority import sanitize_v8_outcome


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'scenario',
    [
        'fresh',
        'expired',
        'missing_checkpoint',
        'changed_scope',
        'revoked',
        'no_threshold',
        'changed_review',
        'no_refs',
    ],
)
async def test_v19_projection_rechecks_source_authority_and_freshness(monkeypatch, scenario):
    evidence_id = str(uuid4())
    citation = {'evidence_ref': evidence_id, 'source_id': 'source-1', 'quote': 'CPU is 80%'}
    draft = {
        'answerable': True,
        'claims': [{'text': 'CPU is 80%', 'citations': [citation]}],
        'missing': [],
    }
    validated = {
        'claims': [
            {
                'text': 'CPU is 80%',
                'citations': [
                    dict(citation, title='CPU guide', source_locator=None, source_uri=None)
                ],
            }
        ]
    }
    answer = KnowledgeAnswer.model_validate({**validated, 'verification': 'passed'})
    payload = {
        'schema_version': 'dod-tool-result-v1',
        'status': 'ok',
        'data': {
            'matches': [
                {
                    'source_id': 'source-1',
                    'title': 'CPU guide',
                    'content': 'CPU is 80%',
                    'updated_at': '2026-01-01T00:00:00Z',
                }
            ]
        },
    }

    async def allowed(*args, **kwargs):
        return ({} if scenario == 'revoked' else {evidence_id: payload}), (
            'changed' if scenario == 'changed_scope' else 'scope'
        )

    monkeypatch.setattr(
        'workbench_agent.runtime.graphs.evidence_authority.authorized_sources', allowed
    )
    state = {
        'draft': draft,
        'scope_id': 'scope',
        'validated_answer': validated,
        'support_review': {
            'checks': [{'index': 0, 'supported': scenario != 'changed_review', 'reason': ''}]
        },
    }
    backend = SimpleNamespace(
        jobs=SimpleNamespace(lock_scope=AsyncMock()),
        lease_guard=SimpleNamespace(require=AsyncMock()),
        runs=SimpleNamespace(
            checkpoint=AsyncMock(
                return_value=None if scenario == 'missing_checkpoint' else {'values': state}
            )
        ),
        clock=SimpleNamespace(
            now_ms=AsyncMock(
                return_value=int(
                    datetime(
                        2026, 1, 2 if scenario == 'expired' else 1, tzinfo=timezone.utc
                    ).timestamp()
                    * 1000
                )
            )
        ),
    )
    guard = SimpleNamespace(team_id='t', run_id='r')
    context = SimpleNamespace(
        graph_id='knowledge.citation_review',
        knowledge_max_age_seconds=None if scenario == 'no_threshold' else 3600,
    )
    outcome = RunOutcome(
        kind='analysis.v1',
        completed=True,
        summary='CPU is 80%',
        findings=(),
        root_cause_hypotheses=(),
        impact=None,
        evidence_refs=() if scenario == 'no_refs' else (evidence_id,),
        unresolved_items=(),
        proposed_actions=(),
        knowledge_gaps=(),
        usage_summary=UsageSummary(),
        knowledge_answer=None if scenario == 'no_refs' else answer,
    )
    result = await sanitize_v8_outcome(
        backend, object(), SimpleNamespace(guard=guard, context=context), outcome
    )
    assert result.completed is (scenario == 'fresh')
    assert (result.knowledge_answer is not None) is (scenario == 'fresh')

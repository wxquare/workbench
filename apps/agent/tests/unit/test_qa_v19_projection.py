from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from workbench_agent.domain.outcomes import RunOutcome, Finding, UsageSummary
from workbench_agent.runtime.graphs.evidence_authority import sanitize_v8_outcome


@pytest.mark.asyncio
@pytest.mark.parametrize('scenario', ['fresh', 'expired', 'missing_checkpoint', 'changed_scope', 'revoked'])
async def test_v19_projection_rechecks_source_authority_and_freshness(monkeypatch, scenario):
    evidence_id = str(uuid4())
    claim = {'text': 'CPU is 80%', 'evidence_ref': evidence_id, 'source_id': 'source-1', 'quote': 'CPU is 80%'}
    answer = {'answerable': True, 'claims': [claim], 'unanswered': [], 'conflicts': []}
    payload = {'schema_version': 'dod-tool-result-v1', 'status': 'ok', 'data': {'matches': [
        {'source_id':'source-1', 'content':'CPU is 80%', 'updated_at':'2026-01-01T00:00:00Z',
         'source_uri':'https://docs.example.test/1'}]}}
    async def allowed(*args, **kwargs):
        return ({} if scenario == 'revoked' else {evidence_id: payload}), ('changed' if scenario == 'changed_scope' else 'scope')
    monkeypatch.setattr('workbench_agent.runtime.graphs.evidence_authority.authorized_sources', allowed)
    backend = SimpleNamespace(jobs=SimpleNamespace(lock_scope=AsyncMock()),
        lease_guard=SimpleNamespace(require=AsyncMock()),
        runs=SimpleNamespace(checkpoint=AsyncMock(return_value=None if scenario == 'missing_checkpoint' else {'values': {'cited_answer':answer,
            'synthesis_scope_id':'scope','verification_complete':True, 'verified_claims':[claim]}})),
        clock=SimpleNamespace(now_ms=AsyncMock(return_value=int(datetime(2026,1,2 if scenario == 'expired' else 1,tzinfo=timezone.utc).timestamp()*1000))))
    guard = SimpleNamespace(team_id='t', run_id='r')
    context = SimpleNamespace(graph_version='dod.v19', knowledge_max_age_seconds=3600)
    outcome = RunOutcome(kind='analysis.v1', completed=True, summary='CPU is 80%',
        findings=(Finding(summary='CPU is 80%', evidence_refs=(evidence_id,)),),
        root_cause_hypotheses=(), impact=None, evidence_refs=(evidence_id,), unresolved_items=(),
        proposed_actions=(), knowledge_gaps=(), usage_summary=UsageSummary())
    result = await sanitize_v8_outcome(backend, object(), SimpleNamespace(guard=guard, context=context), outcome)
    if scenario == 'fresh':
        assert result.completed is True
        assert result.findings
    else:
        assert result.completed is False
        assert result.findings == ()
        assert result.unresolved_items

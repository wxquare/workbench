from workbench_agent.domain.outcomes import RunOutcome


def test_old_outcome_canonical_and_digest_remain_unchanged():
    old = RunOutcome.model_validate(
        {
            'kind': 'analysis.v1',
            'completed': False,
            'summary': 'Incomplete',
            'findings': [],
            'root_cause_hypotheses': [],
            'impact': None,
            'evidence_refs': [],
            'unresolved_items': ['Missing evidence'],
            'proposed_actions': [],
            'knowledge_gaps': [],
            'usage_summary': {'status': 'pending'},
        }
    )
    assert old.digest() == '5091010da166bfd575a6f0ea5d90e33bf02e7790f2c2d6192e0311a191d8e92d'
    assert 'knowledge_answer' not in old.canonical()

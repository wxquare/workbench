"""Read-time evidence disclosure. Stored results remain immutable."""
from types import SimpleNamespace
from workbench_agent.domain.context import RunContext
from workbench_agent.runtime.graphs.evidence_authority import authorized_sources


def reader_context(run, auth):
    """Retain frozen capabilities but authorize the actual current reader."""
    return RunContext.model_validate_json(run['versions']).model_copy(update={
        'subject': auth.subject, 'roles': auth.roles, 'identity_kind': auth.identity_kind,
        'agent_scope': auth.agent_scope,
    })


async def readable_evidence(backend, uow, run, auth, refs, *, include_truncated=False):
    context = reader_context(run, auth)
    guard = SimpleNamespace(team_id=auth.team_id, run_id=run['run_id'])
    return (await authorized_sources(backend, uow, guard, context, refs, include_truncated=include_truncated))[0]


async def project_outcome(backend, uow, run, auth, outcome):
    if outcome is None:
        return None
    refs = set(outcome.get('evidence_refs') or ())
    if not refs:
        return outcome
    allowed = await readable_evidence(backend, uow, run, auth, refs)
    if set(allowed) == refs:
        return outcome
    value = dict(outcome)
    value.update(completed=False, summary='Analysis is unavailable because evidence scope or permissions changed.',
                 findings=[], root_cause_hypotheses=[], impact=None, evidence_refs=[], proposed_actions=[],
                 knowledge_gaps=[], knowledge_answer=None, unresolved_items=['Evidence is not available under your current scope or permissions.'])
    return value

"""Revalidate evidence provenance and current permissions within a caller UoW."""
from workbench_agent.runtime.graphs.ids import GraphId
import json
from workbench_agent.application.idempotency import digest
from workbench_agent.domain.profiles import VersionRef
from workbench_agent.tools.policy import load_published_binding
from workbench_agent.tools.mcp import ConnectionPolicy, DOD_MCP_TOOLS
from workbench_agent.tools.scope_mapping import validate_mapping


async def authorized_sources(backend, uow, guard, context, references, expected_scope=None, *, include_truncated=False):
    scope = await backend.runs.current_scope(uow, guard.team_id, guard.run_id)
    if not scope or (expected_scope is not None and scope['scope_id'] != expected_scope):
        return {}, scope['scope_id'] if scope else None
    scope_id = scope['scope_id']
    permissions = getattr(backend, '_tool_permissions', None)
    if not permissions:
        return {}, scope_id
    refs = set(references)
    if not refs:
        return {}, scope_id
    rows = await backend.results.evidence(uow, guard.team_id, guard.run_id, refs)
    invocations = await backend.tools.succeeded_invocations(uow, guard.team_id, guard.run_id)
    receipts = {}
    for invocation in invocations:
        try:
            receipt = json.loads(invocation['receipt_ref'])
            evidence_id = receipt.get('evidence_id')
            if evidence_id in refs:
                receipts.setdefault(evidence_id, []).append((invocation, receipt))
        except (ValueError, TypeError, AttributeError):
            continue
    result = {}
    for row in rows:
        evidence_id = row['evidence_id']
        linked = receipts.get(evidence_id, [])
        if len(linked) != 1 or row['scope_id'] != scope_id or row['quality'] not in (('valid', 'truncated') if include_truncated else ('valid',)):
            continue
        invocation, receipt = linked[0]
        if receipt.get('scope_id') != scope_id:
            continue
        try:
            binding = None
            if receipt.get('kind') == 'snapshot-evidence-v1':
                ref = VersionRef.model_validate(receipt['snapshot'])
                if (ref not in context.repositories or invocation['tool_version'] != ref.resource_id
                        or row['source'] != 'repository.read'):
                    continue
                authority = ('code', 'repository.read')
            elif receipt.get('kind') == 'mcp-evidence-v1':
                ref = VersionRef.model_validate(receipt['binding'])
                if ref not in context.tools or invocation['tool_version'] != ref.resource_id:
                    continue
                binding_row = await backend.configuration.one(uow, 'tool_bindings', team_id=context.team_id,
                    binding_id=ref.resource_id,
                    version=int(ref.version), hash=ref.content_hash)
                if not binding_row:
                    continue
                raw = json.loads(binding_row['schema'])
                binding = load_published_binding(raw) if digest(raw) == ref.content_hash else None
                if not binding or not binding.read_only or row['source'] != binding.alias:
                    continue
                if binding.argument_mapping is not None:
                    current_scope = json.loads(scope['scope_json'])
                    policy = getattr(backend, '_mcp_connection_policy', None)
                    if not isinstance(policy, ConnectionPolicy):
                        continue
                    signed = []
                    for rule in policy._bindings():
                        if (
                            rule.team_id == context.team_id
                            and rule.agent_id == context.agent_id
                            and rule.server == binding.server
                            and rule.credential_scope == binding.credential_scope
                            and binding.tool_name in rule.read_tools
                        ):
                            for item in rule.mapping_policies:
                                if (
                                    isinstance(item, dict)
                                    and item.get('tool_name') == binding.tool_name
                                ):
                                    signed.append(item)
                    if len(signed) != 1:
                        continue
                    try:
                        validate_mapping(
                            binding.argument_mapping,
                            binding.input_schema,
                            signed[0],
                        )
                    except (ValueError, TypeError):
                        continue
                    if not any(
                        binding.tool_name in rule.tool_conversation_allowed_tools
                        for rule in policy._bindings()
                        if (
                            rule.team_id == context.team_id
                            and rule.agent_id == context.agent_id
                            and rule.server == binding.server
                            and rule.credential_scope == binding.credential_scope
                        )
                    ):
                        continue
                if context.graph_id in (
                    GraphId.KNOWLEDGE_SCOPED_EXCERPT,
                    GraphId.KNOWLEDGE_CITATION_REVIEW,
                    GraphId.CONVERSATION_TOOL_USE,
                ): expert = 'knowledge'
                elif context.graph_id in (
                    GraphId.SOP_CONDITIONAL,
                    GraphId.RCA_SYNTHETIC,
                    GraphId.RCA_PROPOSAL,
                    GraphId.RCA_BATCH_PROPOSAL,
                ): expert = 'diagnosis'
                else: continue
                authority = (expert, binding.permission)
            else:
                continue
            if not permissions.allows(context, authority[1], authority[0]):
                continue
            payload = await backend.tools.evidence_payload(uow, guard.team_id, guard.run_id, evidence_id)
            if not payload:
                continue
            truncated = bool(payload['truncated'])
            if row['quality'] == 'valid' and truncated:
                continue
            if row['quality'] == 'truncated' and (not include_truncated or not truncated):
                continue
            value = json.loads(payload['payload'])
            if (
                binding is not None
                and binding.argument_mapping is not None
                and not truncated
                and digest(value) != row['summary']
            ):
                continue
            if context.graph_id == GraphId.KNOWLEDGE_CITATION_REVIEW:
                # Knowledge receipts bind the frozen scope but carry no time window.
                # Verify the immutable payload and each upstream reference again.
                current = json.loads(scope['scope_json'])
                upstream = value.get('evidence_refs') if isinstance(value, dict) else None
                if (receipt.get('kind') != 'mcp-evidence-v1' or binding.tool_name != 'knowledge_search'
                        or digest(value) != row['summary'] or value.get('status') != 'ok'
                        or not isinstance(upstream, list) or not upstream):
                    continue
                if any(not isinstance(item, dict) or not isinstance(item.get('scope'), dict)
                       or any(item['scope'].get(k) != current[k] for k in ('service', 'environment', 'region'))
                       or item.get('invocation_id') != invocation['logical_call_id']
                       or item.get('query_scope_ref') != scope_id or item.get('source_type') != 'knowledge'
                       or item.get('quality_status') != 'valid' or not item.get('source_locator')
                       for item in upstream):
                    continue
                from workbench_agent.tools.mcp import permits_knowledge_evidence
                policy = getattr(backend, '_mcp_connection_policy', None)
                if any(not permits_knowledge_evidence(policy, team_id=context.team_id, agent_id=context.agent_id,
                        binding=binding, query_scope=current, run_id=guard.run_id,
                        invocation_id=invocation['logical_call_id'], scope_id=scope_id,
                        upstream_scope=item['scope']) for item in upstream):
                    continue
            if context.graph_id in (GraphId.RCA_SYNTHETIC,GraphId.RCA_PROPOSAL,GraphId.RCA_BATCH_PROPOSAL) or (context.graph_id == GraphId.SOP_CONDITIONAL and receipt.get('kind') == 'mcp-evidence-v1' and binding.tool_name in DOD_MCP_TOOLS):
                # Pre-binding v10 receipts did not prove the business team.
                # Keep them unavailable instead of guessing their provenance.
                from datetime import datetime, timezone
                business = receipt.get('business_scope')
                current = json.loads(scope['scope_json'])
                if (receipt.get('kind') != 'mcp-evidence-v1' or not isinstance(business, list)
                        or len(business) != 6 or any(not isinstance(v, str) or not v.strip() for v in business)
                        or business[1:4] != [current['environment'], current['region'], current['service']]):
                    continue
                def utc(raw):
                    parsed = datetime.fromisoformat(raw.replace('Z', '+00:00'))
                    if parsed.tzinfo is None or parsed.utcoffset() is None: raise ValueError()
                    return parsed.astimezone(timezone.utc).isoformat()
                if ([utc(current['start_at']), utc(current['end_at'])] != business[4:6]
                        or json.loads(row['time_window']) != {k:current[k] for k in ('start_at', 'end_at')}):
                    continue
                if not truncated:
                    from workbench_agent.tools.mcp import dod_business_identity
                    upstream_refs = value.get('evidence_refs')
                    if not isinstance(upstream_refs, list) or not upstream_refs:
                        continue
                    if any(list(dod_business_identity({'scope': item['scope'], **item['time_window']})) != business
                           for item in upstream_refs):
                        continue
            result[evidence_id] = value
        except (ValueError, TypeError, KeyError, PermissionError, AttributeError):
            continue
    return result, scope_id


async def sanitize_v8_outcome(backend, uow, claim, outcome):
    """Discard inaccessible evidence and text atomically before projection."""
    if claim.context.graph_id not in (
        GraphId.RCA_SYNTHETIC,
        GraphId.RCA_PROPOSAL,
        GraphId.RCA_BATCH_PROPOSAL,
        GraphId.KNOWLEDGE_SCOPED_EXCERPT,
        GraphId.KNOWLEDGE_CITATION_REVIEW,
        GraphId.CONVERSATION_TOOL_USE,
    ):
        return outcome
    if not outcome.evidence_refs and not (claim.context.graph_id == GraphId.KNOWLEDGE_CITATION_REVIEW and outcome.completed):
        return outcome
    await backend.jobs.lock_scope(uow, claim.guard)
    await backend.lease_guard.require(uow, claim.guard, {'running', 'finalizing'})
    refs = {str(ref) for ref in outcome.evidence_refs}
    allowed, scope_id = await authorized_sources(backend, uow, claim.guard, claim.context, refs)
    safe = bool(refs) and set(allowed) == refs
    if safe and claim.context.graph_id == GraphId.KNOWLEDGE_CITATION_REVIEW and outcome.completed:
        from datetime import datetime, timezone
        from workbench_agent.domain.knowledge_answers import AnswerDraft, KnowledgeAnswer, SupportReview
        from workbench_agent.knowledge.answer_validation import collect_passages, validate_answer, validate_freshness
        try:
            saved = await backend.runs.checkpoint(uow, claim.guard.team_id, claim.guard.run_id)
            state = saved['values']
            draft = AnswerDraft.model_validate(state['draft'])
            sources = collect_passages(allowed)
            verified = validate_answer(draft, sources)
            review = SupportReview.model_validate(state['support_review'])
            now = datetime.fromtimestamp((await backend.clock.now_ms(uow)) / 1000, timezone.utc)
            validate_freshness(draft, sources, now=now, max_age_seconds=claim.context.knowledge_max_age_seconds)
            expected = KnowledgeAnswer.model_validate({**verified.model_dump(mode='json'), 'verification': 'passed'})
            safe = (bool(scope_id) and scope_id == state.get('scope_id') and review.approves(len(verified.claims))
                    and outcome.knowledge_answer == expected and state.get('validated_answer') == verified.model_dump(mode='json'))
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            safe = False
    if safe:
        return outcome
    from workbench_agent.domain.outcomes import RunOutcome
    value = outcome.model_dump(mode='json')
    value.update(completed=False, summary='Analysis is incomplete because evidence scope or permissions changed.',
                 findings=[], root_cause_hypotheses=[], impact=None, evidence_refs=[], proposed_actions=[],
                 unresolved_items=['Evidence is no longer available under the current scope or permissions.'], knowledge_answer=None)
    return RunOutcome.model_validate(value)

"""Pinned scoped QA graph with source-bound cited synthesis."""
from dataclasses import asdict
from typing import Annotated, TypedDict

from langchain_core.messages import AnyMessage, HumanMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.runtime import Runtime

STATE_SCHEMA_VERSION = 'dod-state-v19'


class DoDState(TypedDict, total=False):
    run_id: str
    input_text: str
    mode: str
    route: str
    phase: str
    messages: Annotated[list[AnyMessage], add_messages]
    unresolved: list[str]
    outcome: dict
    validation: dict
    evidence_refs: list[str]
    diagnosis_done: bool
    cited_answer: dict | None
    synthesis_scope_id: str | None
    synthesis_error: str | None
    verified_claims: list[dict]
    verification_complete: bool
    verification_reasons: list[str]


def build_dod(saver):
    async def prepare_context(state, config, runtime: Runtime):
        await runtime.context.check_operation()
        if state['run_id'] != config['configurable']['thread_id']:
            raise ValueError('graph thread_id must equal run_id')
        return {'phase': 'prepare_context', 'messages': [HumanMessage(state['input_text'], id=state['run_id'] + ':input')]}

    async def route(state, runtime: Runtime):
        await runtime.context.check_operation()
        mode = state['mode']
        # No model classifier is enabled: ambiguous input stays unresolved.
        return {'phase': 'route', 'route': mode}

    async def select_skills(state, runtime: Runtime):
        await runtime.context.check_operation()
        return {'phase': 'select_skills'}

    async def collect_baseline(state, runtime: Runtime):
        await runtime.context.check_operation()
        return {'phase': 'collect_baseline'}

    async def investigate(state, runtime: Runtime):
        await runtime.context.check_operation()
        execution = runtime.context
        factory = getattr(execution.backend, '_diagnosis_factory', None)
        context = execution.claim.context
        if state['route'] not in ('qa',) or factory is None:
            return {'phase': 'investigate', 'unresolved': [
                'Required expert execution and verifiable evidence are not available for this Run.'
            ], 'evidence_refs': [], 'diagnosis_done': False}
        import json
        from workbench_agent.application.idempotency import digest
        from workbench_agent.tools.policy import load_published_binding
        bindings = []
        async with execution.backend.create_uow().begin_read() as uow:
            scope = await execution.backend.runs.current_scope(uow, execution.guard.team_id, state['run_id'])
            if scope:
                for ref in context.tools:
                    row = await execution.backend.configuration.one(uow, 'tool_bindings', team_id=context.team_id,
                        binding_id=ref.resource_id, agent_id=context.agent_id,
                        version=int(ref.version), hash=ref.content_hash)
                    if row:
                        raw = json.loads(row['schema'])
                        if digest(raw) == ref.content_hash:
                            binding = load_published_binding(raw)
                            if binding and binding.read_only and binding.tool_name == 'knowledge_search':
                                from workbench_agent.tools.mcp import BUSINESS_TOOLS, DOD_MCP_TOOLS
                                if binding.tool_name not in BUSINESS_TOOLS or binding.tool_name in DOD_MCP_TOOLS or binding.tool_name in ('SRE-Log-Query', 'Common-DoD-GetInfo'):
                                    bindings.append((ref, binding))
        if not bindings or len(bindings) > 4:
            return {'phase': 'investigate', 'unresolved': ['A scoped, published knowledge tool is unavailable.'],
                    'evidence_refs': [], 'diagnosis_done': False}
        from workbench_agent.runtime.experts.knowledge import KnowledgeExpert, ExpertTask
        from workbench_agent.application.errors import DomainError
        from workbench_agent.jobs.worker import CapabilityBlocked
        try:
            with execution._tool_owner.expert_execution(execution, 'knowledge') as child:
                invoker, executor = factory(child)
                expert = KnowledgeExpert(child, invoker, executor, bindings,
                    saver=saver, guard_provider=saver.guard_provider)
                result = await expert.invoke(ExpertTask(task_id='baseline', instruction=state['input_text']))
        except ValueError:
            return {'phase': 'investigate', 'unresolved': ['Knowledge tool schema is unavailable.'],
                    'evidence_refs': [], 'diagnosis_done': False}
        except DomainError as error:
            if error.code not in ('BUDGET_EXCEEDED', 'MAX_CONTEXT_EXCEEDED', 'SUMMARY_RESERVE_REQUIRED', 'MODEL_PROFILE_UNAVAILABLE', 'INVOCATION_ALREADY_SENT', 'RESERVATION_CONFLICT', 'STALE_QUERY_SCOPE'):
                raise
            if error.code == 'STALE_QUERY_SCOPE':
                reason = 'Query scope changed; prior model or tool results cannot be reused under the current scope.'
            elif error.code in ('INVOCATION_ALREADY_SENT', 'RESERVATION_CONFLICT'):
                reason = 'A previously dispatched model response could not be recovered; it was not resent.'
            elif error.code == 'MODEL_PROFILE_UNAVAILABLE':
                reason = 'The configured knowledge model is unavailable.'
            else:
                reason = 'Knowledge could not run within the remaining budget.'
            return {'phase': 'investigate', 'unresolved': [reason],
                    'evidence_refs': [], 'diagnosis_done': False}
        except CapabilityBlocked:
            return {'phase': 'investigate', 'unresolved': ['Knowledge capability is unavailable.'],
                    'evidence_refs': [], 'diagnosis_done': False}
        # The agent's free text is not an evidence-backed business finding.
        return {'phase': 'investigate', 'unresolved': [] if result.evidence_refs else ['No verifiable knowledge evidence was found.'],
                'evidence_refs': list(result.evidence_refs), 'diagnosis_done': True}

    async def validate_evidence(state, runtime: Runtime):
        await runtime.context.check_operation()
        from workbench_agent.runtime.graphs.validation import EvidenceValidator
        async with runtime.context.backend.create_uow().begin_read() as uow:
            scope = await runtime.context.backend.runs.current_scope(
                uow, runtime.context.guard.team_id, state['run_id'])
            rows = await runtime.context.backend.results.evidence(uow, runtime.context.guard.team_id,
                state['run_id'], set(state.get('evidence_refs', []))) if state.get('evidence_refs') else []
            evidence = {}
            for row in rows:
                value = dict(row)
                payload = await runtime.context.backend.tools.evidence_payload(uow, runtime.context.guard.team_id,
                    state['run_id'], value['evidence_id'])
                value['reference_accessible'] = bool(payload and not payload['truncated'])
                evidence[value['evidence_id']] = value
        completed = {'investigate'} if state.get('diagnosis_done') else set()
        result = EvidenceValidator().validate(scope['scope_id'] if scope else None, evidence,
            {'investigate'}, completed_steps=completed, referenced_ids=set(state.get('evidence_refs', [])))
        return {'phase': 'validate_evidence', 'validation': asdict(result)}

    async def synthesize_answer(state, runtime: Runtime):
        from workbench_agent.runtime.graphs.qa_v19_synthesis import synthesize
        return await synthesize(state, runtime.context)

    async def verify_answer(state, runtime: Runtime):
        from workbench_agent.runtime.graphs.qa_v19_synthesis import verify
        return await verify(state, runtime.context)

    async def compose_outcome(state, runtime: Runtime):
        from workbench_agent.runtime.graphs.qa_v19_synthesis import compose
        return await compose(state, runtime.context)

    async def finalize(state, runtime: Runtime):
        await runtime.context.check_operation()
        return {'phase': 'finalize'}

    graph = StateGraph(DoDState)
    nodes = (prepare_context, route, select_skills, collect_baseline, investigate,
             validate_evidence, synthesize_answer, verify_answer, compose_outcome, finalize)
    for node in nodes:
        graph.add_node(node.__name__, node)
    graph.add_edge(START, nodes[0].__name__)
    for left, right in zip(nodes, nodes[1:]):
        graph.add_edge(left.__name__, right.__name__)
    graph.add_edge(nodes[-1].__name__, END)
    return graph.compile(checkpointer=saver)

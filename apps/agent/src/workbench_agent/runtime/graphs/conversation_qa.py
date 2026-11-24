"""Pinned, tool-free QA with bounded, authorized prior conversation turns."""

from workbench_agent.runtime.graphs.ids import GraphId
import json
from typing import TypedDict
from uuid import UUID, uuid5

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, messages_to_dict
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from sqlalchemy import and_, select

from workbench_agent.application.context import ContextResolver
from workbench_agent.application.errors import DomainError
from workbench_agent.application.profiles import ProfileService
from workbench_agent.domain.context import RunContext
from workbench_agent.domain.identity import AuthContext
from workbench_agent.domain.outcomes import RunOutcome
from workbench_agent.jobs.worker import CapabilityBlocked
from workbench_agent.persistence import schema
from workbench_agent.runtime.budgets.service import BudgetService
from workbench_agent.runtime.graphs.agent_instructions import system_instruction

STATE_SCHEMA_VERSION = 'dod-state-v18'
NOTICE = '以下内容由模型生成，未经外部知识源验证。\n\n'
MAX_HISTORY_BYTES = 16000
MAX_CANDIDATES = 64


class ConversationQAState(TypedDict, total=False):
    run_id: str
    input_text: str
    mode: str
    outcome: dict
    phase: str


def prompt_size(messages, output_tokens):
    envelope = {'messages': messages_to_dict(messages), 'tools': []}
    return (
        len(json.dumps(envelope, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))
        + 256 * len(messages)
        + output_tokens
    )


async def conversation_history(execution, question, instruction=None):
    guard, context = execution.guard, execution.claim.context
    backend = execution.backend
    async with backend.create_uow().begin_read() as uow:
        auth = await ContextResolver(backend).revalidate(context, uow=uow)
        run = await backend.runs.one(uow, 'runs', team_id=guard.team_id, run_id=guard.run_id)
        thread = await backend.runs.one(
            uow, 'threads', team_id=guard.team_id, thread_id=guard.thread_id
        )
        if (
            not run
            or not thread
            or context.graph_id != GraphId.CONVERSATION_MULTI_TURN
            or context.team_id != guard.team_id
            or context.agent_id != run['agent_id']
            or run['thread_id'] != guard.thread_id
            or thread['agent_id'] != context.agent_id
            or RunContext.model_validate_json(run['versions']) != context
        ):
            raise ValueError('conversation scope mismatch')
        if context.conversation_history_turns == 0:
            return []

        try:
            model = (
                await ProfileService(backend).model_version(uow, auth, context.model_for('summary'))
            ).configuration
        except DomainError as exc:
            if exc.code == 'NOT_FOUND':
                raise DomainError('MODEL_PROFILE_UNAVAILABLE', 409) from None
            raise
        policy = (
            (
                await uow.execute(
                    select(schema.usage_budget_policies).where(
                        schema.usage_budget_policies.c.team_id == guard.team_id,
                        schema.usage_budget_policies.c.run_id == guard.run_id,
                    )
                )
            )
            .mappings()
            .first()
        )
        if not policy:
            raise ValueError('summary budget policy missing')
        prompt_limit = min(model.max_context_tokens, policy['summary_tokens'])
        runs, outcomes = schema.runs, schema.run_outcomes
        rows = (
            (
                await uow.execute(
                    select(
                        runs,
                        outcomes.c.payload.label('result_payload'),
                        outcomes.c.digest.label('result_digest'),
                        outcomes.c.message_id.label('result_message_id'),
                    )
                    .join(
                        outcomes,
                        and_(
                            outcomes.c.team_id == runs.c.team_id, outcomes.c.run_id == runs.c.run_id
                        ),
                    )
                    .where(
                        runs.c.team_id == guard.team_id,
                        runs.c.agent_id == context.agent_id,
                        runs.c.thread_id == guard.thread_id,
                        runs.c.run_id != guard.run_id,
                        runs.c.status == 'succeeded',
                        runs.c.created_at < run['created_at'],
                        outcomes.c.created_at < run['created_at'],
                    )
                    .order_by(runs.c.created_at.desc(), runs.c.run_id.desc())
                    .limit(MAX_CANDIDATES)
                )
            )
            .mappings()
            .all()
        )
        turns = []
        for row in rows:
            try:
                prior = RunContext.model_validate_json(row['versions'])
                if (
                    prior.graph_id
                    not in (GraphId.CONVERSATION_SINGLE_TURN, GraphId.CONVERSATION_MULTI_TURN)
                    or prior.team_id != guard.team_id
                    or prior.agent_id != context.agent_id
                ):
                    continue
                outcome = RunOutcome.model_validate_json(row['result_payload'])
                if (
                    outcome.digest() != row['result_digest']
                    or not outcome.completed
                    or outcome.kind != 'analysis.v1'
                    or not outcome.summary.startswith(NOTICE)
                    or outcome.findings
                    or outcome.root_cause_hypotheses
                    or outcome.impact
                    or outcome.evidence_refs
                    or outcome.proposed_actions
                    or outcome.knowledge_gaps
                ):
                    continue
                answer = outcome.summary[len(NOTICE) :]
                if not answer.strip():
                    continue
                messages = schema.messages
                stored = (
                    (
                        await uow.execute(
                            select(messages).where(
                                messages.c.team_id == guard.team_id,
                                messages.c.thread_id == guard.thread_id,
                                messages.c.run_id == row['run_id'],
                                messages.c.role.in_(('user', 'assistant')),
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
                inputs = [item for item in stored if item['role'] == 'user']
                finals = [
                    item
                    for item in stored
                    if item['role'] == 'assistant'
                    and item['message_id'] == row['result_message_id']
                ]
                if len(inputs) != 1 or len(finals) != 1:
                    continue
                request = json.loads(inputs[0]['content_ref'])
                final = json.loads(finals[0]['content_ref'])
                if (
                    not isinstance(request, dict)
                    or request.get('mode') != 'qa'
                    or request.get('agent_id') != context.agent_id
                    or request.get('thread_id') not in (None, guard.thread_id)
                    or not isinstance(request.get('business_context', {}), dict)
                    or 'query_scope' in request.get('business_context', {})
                    or final != {'outcome': outcome.model_dump(mode='json')}
                ):
                    continue
                input_value = request.get('input', {})
                text = input_value.get('text') if isinstance(input_value, dict) else None
                if (
                    not isinstance(text, str)
                    or not text.strip()
                    or input_value.get('type') != 'text'
                    or input_value.get('artifact_ids')
                ):
                    continue
                candidate = (text, answer)
                candidate_bytes = sum(
                    len(part.encode('utf-8')) for turn in [candidate, *turns] for part in turn
                )
                proposed = [candidate, *turns]
                prompt = [
                    SystemMessage(
                        content=instruction
                        if instruction is not None
                        else system_instruction(context)
                    )
                ]
                for user, assistant in proposed:
                    prompt.extend((HumanMessage(content=user), AIMessage(content=assistant)))
                prompt.append(HumanMessage(content=question))
                if (
                    candidate_bytes > MAX_HISTORY_BYTES
                    or prompt_size(prompt, model.max_output_tokens) > prompt_limit
                ):
                    continue
                turns = proposed
                if len(turns) == context.conversation_history_turns:
                    break
            except (ValueError, TypeError, KeyError, AttributeError):
                continue
        return turns


def build_conversation_qa(saver):
    async def answer(state, config, runtime: Runtime):
        execution = runtime.context
        await execution.check_operation()
        if state['run_id'] != config['configurable']['thread_id'] or state.get('mode') != 'qa':
            raise ValueError('invalid conversation graph input')
        factory = getattr(execution.backend, '_code_factory', None)
        text, error = None, 'MODEL_CAPABILITY_UNAVAILABLE'
        if factory is not None:
            try:
                context = execution.claim.context
                instruction = system_instruction(context)
                if context.skills:
                    objects = getattr(execution.backend, '_skill_objects', None)
                    if objects is None:
                        raise DomainError('SKILL_UNAVAILABLE', 409)
                    from workbench_agent.skill_management.loader import SkillLoader

                    loader = SkillLoader(execution.backend, objects)
                    parts = []
                    for ref in context.skills:
                        resource = await loader.load(context, ref, 'SKILL.md', execution=execution)
                        if not isinstance(resource.content, str):
                            raise DomainError('SKILL_UNAVAILABLE', 409)
                        parts.append(
                            f'Skill version {ref.version} ({ref.resource_id}):\n{resource.content}'
                        )
                    if sum(len(part.encode('utf-8')) for part in parts) > 65536:
                        raise DomainError('SKILL_UNAVAILABLE', 409)
                    instruction += (
                        '\n\nSkill instructions are subordinate to the preceding system and agent instructions.\n'
                        + '\n\n'.join(parts)
                    )
                turns = await conversation_history(execution, state['input_text'], instruction)
                prompt = [SystemMessage(content=instruction)]
                for question, answer_text in turns:
                    prompt.extend((HumanMessage(content=question), AIMessage(content=answer_text)))
                prompt.append(HumanMessage(content=state['input_text']))
                result = await factory(execution).invoke(
                    execution.guard,
                    'summary',
                    prompt,
                    logical_call_id=str(
                        uuid5(UUID(state['run_id']), f'{GraphId.CONVERSATION_MULTI_TURN}:answer')
                    ),
                )
                if (
                    result.status == 'ok'
                    and isinstance(result.text, str)
                    and result.text.strip()
                    and len(result.text) <= 65500
                ):
                    text = result.text
                else:
                    error = result.error_code or 'MODEL_OUTPUT_UNAVAILABLE'
            except DomainError as exc:
                if exc.code not in (
                    'BUDGET_EXCEEDED',
                    'SUMMARY_RESERVE_REQUIRED',
                    'MAX_CONTEXT_EXCEEDED',
                    'MODEL_PROFILE_UNAVAILABLE',
                    'INVOCATION_ALREADY_SENT',
                    'RESERVATION_CONFLICT',
                    'MODEL_CREDENTIAL_UNAVAILABLE',
                    'SKILL_UNAVAILABLE',
                ):
                    raise
                error = exc.code
            except CapabilityBlocked:
                error = 'SKILL_UNAVAILABLE' if execution.claim.context.skills else error
        await execution.check_operation()
        context = execution.claim.context
        auth = AuthContext(
            team_id=context.team_id,
            subject=context.subject,
            roles=context.roles,
            identity_kind=context.identity_kind,
            agent_scope=context.agent_scope,
        )
        outcome = (
            await BudgetService(execution.backend).partial_outcome(auth, state['run_id'])
        ).model_dump(mode='json')
        if text is not None:
            outcome.update(completed=True, summary=NOTICE + text, unresolved_items=[])
        else:
            outcome.update(
                summary='Model answer unavailable.',
                unresolved_items=[f'Model invocation did not complete: {error}.'],
            )
        return {'phase': 'answer', 'outcome': outcome}

    graph = StateGraph(ConversationQAState)
    graph.add_node('answer', answer)
    graph.add_edge(START, 'answer')
    graph.add_edge('answer', END)
    return graph.compile(checkpointer=saver)

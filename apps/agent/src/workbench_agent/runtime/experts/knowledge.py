"""A budgeted LangChain knowledge loop bound to a current Worker execution.

Knowledge authority is issued only by the Worker for a pinned QA Run.
"""
from workbench_agent.runtime.graphs.ids import GraphId
from contextvars import ContextVar
from dataclasses import dataclass
import json
import re
from typing import Annotated, Literal
from uuid import UUID, uuid5

from langchain.agents import create_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import InjectedToolCallId, StructuredTool
from pydantic import PrivateAttr, create_model

from workbench_agent.jobs.worker import CapabilityBlocked
from workbench_agent.domain.tools import ToolError, ToolResult
from workbench_agent.runtime.models.invoker import ModelInvoker
from workbench_agent.tools.policy import PolicyToolExecutor


@dataclass(frozen=True)
class ExpertTask:
    task_id: str
    instruction: str


@dataclass(frozen=True)
class ExpertResult:
    subtask_id: str
    text: str
    evidence_refs: tuple[str, ...]
    tool_statuses: tuple[str, ...] = ()


class KnowledgeChatModel(BaseChatModel):
    _invoker: ModelInvoker = PrivateAttr()
    _guard: object = PrivateAttr()
    _tools: tuple = PrivateAttr(default=())
    _subtask_id: str = PrivateAttr()

    def __init__(self, invoker, guard, subtask_id, **kwargs):
        super().__init__(**kwargs)
        self._invoker, self._guard, self._subtask_id = invoker, guard, subtask_id

    @property
    def _llm_type(self):
        return 'dod-budgeted-knowledge'

    def bind_tools(self, tools, **kwargs):
        bound = self.model_copy()
        bound._tools = tuple(tools)
        return bound

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise NotImplementedError('only asynchronous model calls are supported')

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        from workbench_agent.application.idempotency import digest
        from langchain_core.messages import messages_to_dict
        logical_id = str(uuid5(UUID(self._subtask_id), 'model:' + digest(messages_to_dict(messages))))
        dispatch_check = None
        execution = self._invoker.execution
        if execution.claim.context.graph_id == GraphId.KNOWLEDGE_CITATION_REVIEW:
            # Tool messages survive checkpoint replay. Their content may be sent
            # only if the current guarded transaction still authorizes the exact
            # persisted evidence represented in every successful message.
            from workbench_agent.application.errors import DomainError
            from workbench_agent.runtime.graphs.evidence_authority import authorized_sources
            evidence = {}
            for message in messages:
                if not isinstance(message, ToolMessage):
                    continue
                try:
                    value = json.loads(message.content)
                    if not isinstance(value, dict): raise ValueError()
                    if value.get('status') == 'ok':
                        ref = value.get('evidence_ref')
                        if not isinstance(ref, str) or not ref or not isinstance(value.get('data'), dict): raise ValueError()
                        if ref in evidence and evidence[ref] != value['data']: raise ValueError()
                        evidence[ref] = value['data']
                    elif value.get('data') is not None:
                        raise ValueError()
                except (TypeError, ValueError):
                    raise DomainError('FORBIDDEN', 403) from None
            async def dispatch_check(uow):
                await execution.backend.lease_guard.require(uow, self._guard, {'running'})
                allowed, _ = await authorized_sources(execution.backend, uow, self._guard,
                    execution.claim.context, evidence)
                if allowed != evidence:
                    raise DomainError('FORBIDDEN', 403)
        result = await self._invoker.invoke(self._guard, 'knowledge', messages,
            tools=list(self._tools) or None, logical_call_id=logical_id, dispatch_check=dispatch_check)
        if result.status != 'ok' or not isinstance(result.message, AIMessage):
            raise CapabilityBlocked(result.error_code or 'MODEL_UNAVAILABLE')
        return ChatResult(generations=[ChatGeneration(message=result.message)])


def _schema_model(name, schema):
    """Only a safe, small JSON schema subset is exposed to the model.

    PolicyToolExecutor always applies the authoritative published schema again.
    """
    from workbench_agent.tools.model_schema import validate_model_schema
    validate_model_schema(schema)
    props, required = schema.get('properties', {}), set(schema.get('required', ()))
    fields = {}
    kinds = {'string': str, 'integer': int, 'number': float, 'boolean': bool}
    for key, spec in props.items():
        if not isinstance(key, str) or spec.get('type') not in kinds:
            raise ValueError('unsupported expert tool schema')
        cls = kinds[spec['type']]
        if 'enum' in spec:
            values = spec['enum']
            if (not isinstance(values, list) or not values or len(values) > 32
                    or any(type(value) is not cls for value in values)):
                raise ValueError('invalid expert tool enum')
            cls = Literal[tuple(values)]
        fields[key] = (cls, ...) if key in required else (cls | None, None)
    fields['tool_call_id'] = (Annotated[str, InjectedToolCallId], ...)
    return create_model(name, **fields)


def model_tool_schema(tool_name, published_schema):
    from workbench_agent.tools.model_schema import project_model_schema
    return project_model_schema(tool_name, published_schema)


class KnowledgeExpert:
    def __init__(self, execution, invoker, executor, bindings, *, saver, guard_provider):
        owner = getattr(execution, '_tool_owner', None)
        if (owner is None or not owner._owns_tool_execution(execution)
                or execution._expert_name != 'knowledge'
                or not isinstance(invoker, ModelInvoker) or invoker.execution is not execution
                or not isinstance(executor, PolicyToolExecutor) or executor.execution is not execution):
            raise ValueError('trusted knowledge execution required')
        if len(bindings) > 4:
            raise ValueError('at most four tools')
        self.execution, self.invoker, self.executor = execution, invoker, executor
        self.saver, self.guard_provider = saver, guard_provider
        self._subtask = ContextVar('knowledge_subtask', default=None)
        self.tools = []
        names = set()
        for ref, binding in bindings:
            if ref not in execution.claim.context.tools or not binding.read_only:
                raise ValueError('tool must be a frozen read-only binding')
            name = re.sub(r'[^a-zA-Z0-9_-]', '_', binding.alias)
            if not name or name in names:
                raise ValueError('duplicate expert tool name')
            names.add(name)
            schema = _schema_model('Tool_' + name, model_tool_schema(binding.tool_name, binding.input_schema))
            async def call(tool_call_id: str, _ref=ref, **kwargs):
                arguments = {key: value for key, value in kwargs.items() if value is not None}
                # A model tool-call ID is stable across a graph replay, so an
                # uncertain prior dispatch cannot be issued a second time.
                subtask = self._subtask.get()
                if subtask is None:
                    raise PermissionError('expert subtask authority required')
                invocation = str(uuid5(UUID(subtask), 'tool:' + tool_call_id))
                result = await executor.execute(execution.claim.context, execution.guard, invocation, _ref, arguments, max_logical_calls=4)
                if (execution.claim.context.graph_id == GraphId.KNOWLEDGE_CITATION_REVIEW
                        and result.status == 'ok' and isinstance(result.data, dict)
                        and (result.data.get('truncated') is True
                             or result.data.get('meta', {}).get('truncated') is True)):
                    # Keep the original receipt for audit, but never forward
                    # incomplete source content as authorized model evidence.
                    result = ToolResult(status='unavailable',
                        error=ToolError(code='KNOWLEDGE_RESULTS_TRUNCATED'))
                return result.model_dump_json()
            description = 'Read-only scoped tool'
            if execution.claim.context.graph_id == GraphId.KNOWLEDGE_CITATION_REVIEW:
                description = (
                    'Read-only knowledge search. Use specific topic keywords; service, environment '
                    'and region are already enforced. If KNOWLEDGE_RESULTS_TRUNCATED is returned, '
                    'issue a narrower query or increase limit up to 10. This is a new query, '
                    'not a retry of the same arguments.'
                )
            self.tools.append(StructuredTool(name=name, description=description, args_schema=schema, coroutine=call))

    async def invoke(self, task: ExpertTask):
        if not task.task_id or not task.instruction:
            raise ValueError('task identity and instruction are required')
        await self.execution.check_operation()
        subtask_id = str(uuid5(UUID(self.execution.guard.run_id), 'knowledge:' + task.task_id))
        from workbench_agent.runtime.checkpoints.namespaced import NamespacedSaver
        config = {'configurable': {'thread_id': self.execution.guard.run_id}, 'recursion_limit': 12}
        saver = NamespacedSaver(self.saver, 'expert:' + subtask_id)
        agent = create_agent(KnowledgeChatModel(self.invoker, self.execution.guard, subtask_id), tools=self.tools,
                             checkpointer=saver, name='knowledge')
        token = self._subtask.set(subtask_id)
        try:
            return await self._invoke_agent(agent, config, task, subtask_id)
        finally:
            self._subtask.reset(token)

    async def _invoke_agent(self, agent, config, task, subtask_id):
        with self.guard_provider.bind(self.execution.guard):
            saved = await agent.aget_state(config)
            if saved.values:
                messages = saved.values.get('messages', [])
                if not messages or not isinstance(messages[0], HumanMessage) or messages[0].content != task.instruction:
                    raise ValueError('expert task identity conflict')
                values = saved.values if not saved.next else await agent.ainvoke(None, config, durability='sync')
            else:
                values = await agent.ainvoke({'messages': [HumanMessage(task.instruction, id=subtask_id + ':input')]}, config, durability='sync')
        messages = values['messages']
        if not messages or not isinstance(messages[-1], AIMessage) or not isinstance(messages[-1].content, str):
            raise CapabilityBlocked('expert did not produce a final text')
        refs = []
        statuses = []
        for message in messages:
            if isinstance(message, ToolMessage):
                try:
                    payload = json.loads(message.content)
                except (TypeError, ValueError):
                    continue
                statuses.append(payload.get('status', 'unknown') if isinstance(payload, dict) else 'unknown')
                if payload.get('status') == 'ok' and payload.get('evidence_ref'):
                    refs.append(payload['evidence_ref'])
        return ExpertResult(subtask_id, messages[-1].content, tuple(dict.fromkeys(refs)), tuple(statuses))

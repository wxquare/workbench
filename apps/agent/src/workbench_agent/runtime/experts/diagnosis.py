"""A budgeted LangChain diagnosis loop bound to a current Worker execution.

Only diagnosis is currently authorized by Worker. Other experts require their
own trusted subgraph identity and are deliberately not synthesized here.
"""

from contextvars import ContextVar
from dataclasses import dataclass
import json
import re
from typing import Annotated
from uuid import UUID, uuid5

from langchain.agents import create_agent
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import InjectedToolCallId, StructuredTool
from pydantic import PrivateAttr, create_model

from workbench_agent.jobs.worker import CapabilityBlocked
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


class BudgetedChatModel(BaseChatModel):
    _invoker: ModelInvoker = PrivateAttr()
    _guard: object = PrivateAttr()
    _tools: tuple = PrivateAttr(default=())
    _subtask_id: str = PrivateAttr()

    def __init__(self, invoker, guard, subtask_id, **kwargs):
        super().__init__(**kwargs)
        self._invoker, self._guard, self._subtask_id = invoker, guard, subtask_id

    @property
    def _llm_type(self):
        return 'dod-budgeted-diagnosis'

    def bind_tools(self, tools, **kwargs):
        bound = self.model_copy()
        bound._tools = tuple(tools)
        return bound

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise NotImplementedError('only asynchronous model calls are supported')

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        from workbench_agent.application.idempotency import digest
        from langchain_core.messages import messages_to_dict

        logical_id = str(
            uuid5(UUID(self._subtask_id), 'model:' + digest(messages_to_dict(messages)))
        )
        result = await self._invoker.invoke(
            self._guard,
            'diagnosis',
            messages,
            tools=list(self._tools) or None,
            logical_call_id=logical_id,
        )
        if result.status != 'ok' or not isinstance(result.message, AIMessage):
            raise CapabilityBlocked(result.error_code or 'MODEL_UNAVAILABLE')
        return ChatResult(generations=[ChatGeneration(message=result.message)])


def _schema_model(name, schema):
    """Only a safe, small JSON schema subset is exposed to the model.

    PolicyToolExecutor always applies the authoritative published schema again.
    """
    if schema.get('type') != 'object' or schema.get('additionalProperties', True) is not False:
        raise ValueError('tool schema must be a closed object')
    props, required = schema.get('properties', {}), set(schema.get('required', ()))
    fields = {}
    kinds = {'string': str, 'integer': int, 'number': float, 'boolean': bool}
    for key, spec in props.items():
        if (
            not isinstance(key, str)
            or not key.isidentifier()
            or key == 'tool_call_id'
            or spec.get('type') not in kinds
        ):
            raise ValueError('unsupported expert tool schema')
        cls = kinds[spec['type']]
        fields[key] = (cls, ...) if key in required else (cls | None, None)
    if not required <= set(fields):
        raise ValueError('invalid required schema')
    fields['tool_call_id'] = (Annotated[str, InjectedToolCallId], ...)
    return create_model(name, **fields)


def model_tool_schema(tool_name, published_schema):
    from workbench_agent.tools.mcp import BUSINESS_TOOLS

    if tool_name == 'SRE-Log-Query':
        return {
            'type': 'object',
            'properties': {'query': {'type': 'string'}, 'limit': {'type': 'integer'}},
            'required': ['query'],
            'additionalProperties': False,
        }
    if tool_name == 'Common-DoD-GetInfo':
        return {'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False}
    local_schemas = {
        'knowledge_search': (
            {
                'query': {'type': 'string'},
                'scene': {'type': 'string'},
                'limit': {'type': 'integer'},
            },
            ['query', 'scene'],
        ),
        'metrics_query': (
            {'template_id': {'type': 'string'}, 'step_seconds': {'type': 'integer'}},
            ['template_id'],
        ),
        'logs_search': (
            {
                'keyword': {'type': 'string'},
                'trace_id': {'type': 'string'},
                'limit': {'type': 'integer'},
            },
            [],
        ),
        'trace_get': ({'trace_id': {'type': 'string'}}, ['trace_id']),
    }
    if tool_name in local_schemas:
        properties, required = local_schemas[tool_name]
        return {
            'type': 'object',
            'properties': properties,
            'required': required,
            'additionalProperties': False,
        }
    if tool_name in BUSINESS_TOOLS:
        raise ValueError('business tool scope mapping unavailable')
    return published_schema


class DiagnosisExpert:
    def __init__(self, execution, invoker, executor, bindings, *, saver, guard_provider):
        owner = getattr(execution, '_tool_owner', None)
        if (
            owner is None
            or not owner._owns_tool_execution(execution)
            or execution._expert_name != 'diagnosis'
            or not isinstance(invoker, ModelInvoker)
            or invoker.execution is not execution
            or not isinstance(executor, PolicyToolExecutor)
            or executor.execution is not execution
        ):
            raise ValueError('trusted diagnosis execution required')
        if len(bindings) > 4:
            raise ValueError('at most four tools')
        self.execution, self.invoker, self.executor = execution, invoker, executor
        self.saver, self.guard_provider = saver, guard_provider
        self._subtask = ContextVar('diagnosis_subtask', default=None)
        self.tools = []
        names = set()
        for ref, binding in bindings:
            if ref not in execution.claim.context.tools or not binding.read_only:
                raise ValueError('tool must be a frozen read-only binding')
            name = re.sub(r'[^a-zA-Z0-9_-]', '_', binding.alias)
            if not name or name in names:
                raise ValueError('duplicate expert tool name')
            names.add(name)
            schema = _schema_model(
                'Tool_' + name, model_tool_schema(binding.tool_name, binding.input_schema)
            )

            async def call(tool_call_id: str, _ref=ref, **kwargs):
                arguments = {key: value for key, value in kwargs.items() if value is not None}
                # A model tool-call ID is stable across a graph replay, so an
                # uncertain prior dispatch cannot be issued a second time.
                subtask = self._subtask.get()
                if subtask is None:
                    raise PermissionError('expert subtask authority required')
                invocation = str(uuid5(UUID(subtask), 'tool:' + tool_call_id))
                result = await executor.execute(
                    execution.claim.context,
                    execution.guard,
                    invocation,
                    _ref,
                    arguments,
                    max_logical_calls=4,
                )
                return result.model_dump_json()

            self.tools.append(
                StructuredTool(
                    name=name,
                    description='Read-only scoped tool',
                    args_schema=schema,
                    coroutine=call,
                )
            )

    async def invoke(self, task: ExpertTask):
        if not task.task_id or not task.instruction:
            raise ValueError('task identity and instruction are required')
        await self.execution.check_operation()
        subtask_id = str(uuid5(UUID(self.execution.guard.run_id), 'diagnosis:' + task.task_id))
        from workbench_agent.runtime.checkpoints.namespaced import NamespacedSaver

        config = {'configurable': {'thread_id': self.execution.guard.run_id}, 'recursion_limit': 12}
        saver = NamespacedSaver(self.saver, 'expert:' + subtask_id)
        agent = create_agent(
            BudgetedChatModel(self.invoker, self.execution.guard, subtask_id),
            tools=self.tools,
            checkpointer=saver,
            name='diagnosis',
        )
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
                if (
                    not messages
                    or not isinstance(messages[0], HumanMessage)
                    or messages[0].content != task.instruction
                ):
                    raise ValueError('expert task identity conflict')
                values = (
                    saved.values
                    if not saved.next
                    else await agent.ainvoke(None, config, durability='sync')
                )
            else:
                values = await agent.ainvoke(
                    {'messages': [HumanMessage(task.instruction, id=subtask_id + ':input')]},
                    config,
                    durability='sync',
                )
        messages = values['messages']
        if (
            not messages
            or not isinstance(messages[-1], AIMessage)
            or not isinstance(messages[-1].content, str)
        ):
            raise CapabilityBlocked('expert did not produce a final text')
        refs = []
        for message in messages:
            if isinstance(message, ToolMessage):
                try:
                    payload = json.loads(message.content)
                except (TypeError, ValueError):
                    continue
                if payload.get('status') == 'ok' and payload.get('evidence_ref'):
                    refs.append(payload['evidence_ref'])
        return ExpertResult(subtask_id, messages[-1].content, tuple(dict.fromkeys(refs)))

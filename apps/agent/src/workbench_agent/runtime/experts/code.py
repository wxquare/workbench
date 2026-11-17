"""Budgeted code expert with an immutable, Run-pinned snapshot reader."""
from contextvars import ContextVar
from typing import Annotated
from uuid import UUID, uuid5

from langchain.agents import create_agent
from langchain_core.tools import InjectedToolCallId, StructuredTool
from pydantic import create_model

from workbench_agent.runtime.experts.knowledge import KnowledgeChatModel, KnowledgeExpert, ExpertTask, ExpertResult
from workbench_agent.runtime.models.invoker import ModelInvoker
from workbench_agent.tools.snapshot_reader import SnapshotReader


class CodeChatModel(KnowledgeChatModel):
    @property
    def _llm_type(self):
        return 'dod-budgeted-code'

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        from workbench_agent.application.idempotency import digest
        from langchain_core.messages import AIMessage, messages_to_dict
        from langchain_core.outputs import ChatGeneration, ChatResult
        from workbench_agent.jobs.worker import CapabilityBlocked
        logical_id = str(uuid5(UUID(self._subtask_id), 'model:' + digest(messages_to_dict(messages))))
        result = await self._invoker.invoke(self._guard, 'code', messages, tools=list(self._tools) or None, logical_call_id=logical_id)
        if result.status != 'ok' or not isinstance(result.message, AIMessage):
            raise CapabilityBlocked(result.error_code or 'MODEL_UNAVAILABLE')
        return ChatResult(generations=[ChatGeneration(message=result.message)])


class CodeExpert(KnowledgeExpert):
    def __init__(self, execution, invoker, *, saver, guard_provider):
        owner = getattr(execution, '_tool_owner', None)
        if (owner is None or not owner._owns_tool_execution(execution) or execution._expert_name != 'code'
                or not isinstance(invoker, ModelInvoker) or invoker.execution is not execution
                or len(execution.claim.context.repositories) != 1):
            raise ValueError('trusted code execution and one pinned snapshot required')
        self.execution, self.invoker = execution, invoker
        self.saver, self.guard_provider = saver, guard_provider
        self._subtask = ContextVar('code_subtask', default=None)
        reader = SnapshotReader(execution.backend, execution)
        ref = execution.claim.context.repositories[0]
        schema = create_model('ReadSnapshot', path=(str, ...), start=(int, ...), end=(int, ...),
                              tool_call_id=(Annotated[str, InjectedToolCallId], ...))
        async def call(path: str, start: int, end: int, tool_call_id: str):
            subtask = self._subtask.get()
            if subtask is None:
                raise PermissionError('expert subtask authority required')
            invocation = str(uuid5(UUID(subtask), 'tool:' + tool_call_id))
            result = await reader.read(ref, path, start, end, invocation_id=invocation)
            import json
            return json.dumps({'status': 'ok', **result}, ensure_ascii=False)
        self.tools = [StructuredTool(name='read_snapshot', description='Read bounded lines of the pinned repository snapshot.', args_schema=schema, coroutine=call)]

    async def invoke(self, task: ExpertTask) -> ExpertResult:
        if not task.task_id or not task.instruction:
            raise ValueError('task identity and instruction are required')
        await self.execution.check_operation()
        subtask_id = str(uuid5(UUID(self.execution.guard.run_id), 'code:' + task.task_id))
        from workbench_agent.runtime.checkpoints.namespaced import NamespacedSaver
        config = {'configurable': {'thread_id': self.execution.guard.run_id}, 'recursion_limit': 12}
        agent = create_agent(CodeChatModel(self.invoker, self.execution.guard, subtask_id), tools=self.tools,
                             checkpointer=NamespacedSaver(self.saver, 'expert:' + subtask_id), name='code')
        token = self._subtask.set(subtask_id)
        try:
            return await self._invoke_agent(agent, config, task, subtask_id)
        finally:
            self._subtask.reset(token)

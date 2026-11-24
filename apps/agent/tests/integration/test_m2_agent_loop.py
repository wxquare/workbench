import pytest
from langchain_core.messages import AIMessage

from workbench_agent.domain.profiles import ProfileDraft
from workbench_agent.domain.tools import ToolBinding
from workbench_agent.runtime.experts.diagnosis import DiagnosisExpert, ExpertTask
from workbench_agent.runtime.models.invoker import ModelInvoker
from workbench_agent.tools.policy import PolicyToolExecutor
from workbench_agent.tools.registry import ToolRegistry


@pytest.mark.asyncio
@pytest.mark.parametrize('n_calls,model_limit', [(1, None), (5, None), (1, 1)])
async def test_diagnosis_uses_real_agent_loop_guarded_tool_and_shared_budget(
    platform, n_calls, model_limit
):
    from support.skills import execution_for_run
    from workbench_agent.tools.permissions import DeploymentToolPermissions
    from workbench_agent.runtime.checkpoints.portable import (
        InvocationGuard,
        PortableCheckpointSaver,
    )
    from workbench_agent.persistence.checkpoints.codec import CheckpointCodec

    binding = ToolBinding(
        alias='logs.search',
        provider='mcp',
        tool_name='local.logs',
        server='logs',
        credential_scope='reader',
        permission='logs.read',
        input_schema={
            'type': 'object',
            'properties': {'service': {'type': 'string'}},
            'required': ['service'],
            'additionalProperties': False,
        },
    )
    ref = await ToolRegistry(platform.backend).publish(
        platform.auth, platform.agent_id, binding, key='expert-tool'
    )
    context = await platform.contexts.resolve(platform.auth, platform.agent_id)
    agent = await platform.profiles.get_agent(platform.auth, platform.agent_id)
    changed = await platform.profiles.save_draft(
        platform.auth,
        platform.agent_id,
        ProfileDraft(
            default_model=context.default_model, graph_id='conversation.multi_turn', tools=(ref,)
        ),
        agent['revision'],
        key='expert-profile',
    )
    await platform.profiles.publish(
        platform.auth, platform.agent_id, changed['revision'], key='expert-publish'
    )
    run = await platform.create_run(summary_token_reserve=8000, model_call_limit=model_limit)
    async with platform.backend.create_uow().begin_write() as uow:
        await platform.contexts.query_scope(uow, platform.auth, run.run_id, {'service': 'app'})
    rules = {
        'users': {platform.auth.subject: ['logs.read']},
        'teams': {platform.auth.team_id: ['logs.read']},
        'agents': {platform.agent_id: ['logs.read']},
        'skills': {'__none__': ['logs.read']},
        'experts': {'diagnosis': ['logs.read']},
    }
    platform.backend._tool_permissions = DeploymentToolPermissions(lambda: rules)
    execution = await execution_for_run(platform, run)

    class Transport:
        calls = 0

        async def call(self, *args, **kwargs):
            self.calls += 1
            return {'observation': 'available'}

    class FakeModel:
        def bind_tools(self, tools):
            return self

        async def ainvoke(self, messages, **kwargs):
            from langchain_core.messages import ToolMessage

            if any(isinstance(m, ToolMessage) for m in messages):
                return AIMessage(
                    content='Inspection finished.',
                    usage_metadata={'input_tokens': 8, 'output_tokens': 3, 'total_tokens': 11},
                )
            return AIMessage(
                content='',
                tool_calls=[
                    {
                        'name': 'logs_search',
                        'args': {'service': 'app'},
                        'id': 'tool-' + str(n),
                        'type': 'tool_call',
                    }
                    for n in range(n_calls)
                ],
                usage_metadata={'input_tokens': 8, 'output_tokens': 3, 'total_tokens': 11},
            )

    class Factory:
        def build(self, *args, **kwargs):
            return FakeModel()

    transport = Transport()
    invoker = ModelInvoker(platform.backend, Factory(), object(), execution=execution)
    tools = PolicyToolExecutor(platform.backend, transport, execution=execution, delays=(0, 0))
    provider = InvocationGuard()
    saver = PortableCheckpointSaver(platform.backend, provider, CheckpointCodec())
    expert = DiagnosisExpert(
        execution, invoker, tools, [(ref, binding)], saver=saver, guard_provider=provider
    )
    if model_limit == 1:
        from workbench_agent.application.errors import DomainError

        with pytest.raises(DomainError) as error:
            await expert.invoke(ExpertTask(task_id='check-app', instruction='Check app'))
        assert error.value.code == 'BUDGET_EXCEEDED'
        usage = await platform.budgets.snapshot(platform.auth, run.run_id)
        assert usage.model_attempts == usage.tool_attempts == transport.calls == 0
        return
    result = await expert.invoke(ExpertTask(task_id='check-app', instruction='Check app'))
    assert result.text == 'Inspection finished.'
    assert len(result.evidence_refs) == min(n_calls, 4)
    assert transport.calls == min(n_calls, 4)
    usage = await platform.budgets.snapshot(platform.auth, run.run_id)
    assert usage.model_attempts == 2 and usage.tool_attempts == min(n_calls, 4)
    from sqlalchemy import select
    from workbench_agent.persistence.schema import graph_checkpoints

    async with platform.backend.create_uow().begin_read() as uow:
        namespaces = set((await uow.execute(select(graph_checkpoints.c.namespace))).scalars())
    assert namespaces == {'expert:' + result.subtask_id}
    replay = await expert.invoke(ExpertTask(task_id='check-app', instruction='Check app'))
    assert replay == result and transport.calls == min(n_calls, 4)
    usage = await platform.budgets.snapshot(platform.auth, run.run_id)
    assert usage.model_attempts == 2 and usage.tool_attempts == min(n_calls, 4)

    with pytest.raises(ValueError, match='task identity conflict'):
        await expert.invoke(ExpertTask(task_id='check-app', instruction='Different task'))
    if n_calls == 1:
        second = await expert.invoke(ExpertTask(task_id='second-check', instruction='Check again'))
        assert second.subtask_id != result.subtask_id
        assert len(second.evidence_refs) == 1 and transport.calls == 2
        usage = await platform.budgets.snapshot(platform.auth, run.run_id)
        assert usage.model_attempts == 4 and usage.tool_attempts == 2

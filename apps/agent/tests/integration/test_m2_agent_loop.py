from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage

from workbench_agent.domain.profiles import ProfileDraft
from workbench_agent.domain.tools import ToolBinding
from workbench_agent.runtime.experts.diagnosis import DiagnosisExpert, ExpertTask
from workbench_agent.runtime.models.invoker import ModelInvoker
from support.jobs import claim
from workbench_agent.tools.policy import PolicyToolExecutor
from workbench_agent.tools.registry import ToolRegistry


@pytest.mark.asyncio
@pytest.mark.parametrize("n_calls,model_limit", [(1, None), (5, None), (1, 1)])
async def test_diagnosis_uses_real_agent_loop_guarded_tool_and_shared_budget(platform, n_calls, model_limit):
    from support.skills import execution_for_run
    from workbench_agent.tools.permissions import DeploymentToolPermissions
    from workbench_agent.runtime.checkpoints.portable import InvocationGuard, PortableCheckpointSaver
    from workbench_agent.persistence.checkpoints.codec import CheckpointCodec
    from workbench_agent.runtime.models.invoker import ModelResult

    binding = ToolBinding(alias='logs.search', provider='mcp', tool_name='local.logs', server='logs',
        credential_scope='reader', permission='logs.read', input_schema={'type': 'object',
        'properties': {'service': {'type': 'string'}}, 'required': ['service'], 'additionalProperties': False})
    ref = await ToolRegistry(platform.backend).publish(platform.auth, platform.agent_id, binding, key='expert-tool')
    context = await platform.contexts.resolve(platform.auth, platform.agent_id)
    agent = await platform.profiles.get_agent(platform.auth, platform.agent_id)
    changed = await platform.profiles.save_draft(platform.auth, platform.agent_id,
        ProfileDraft(default_model=context.default_model, graph_version='dod.v4', tools=(ref,)),
        agent['revision'], key='expert-profile')
    await platform.profiles.publish(platform.auth, platform.agent_id, changed['revision'], key='expert-publish')
    run = await platform.create_run(summary_token_reserve=8000, model_call_limit=model_limit)
    async with platform.backend.create_uow().begin_write() as uow:
        await platform.contexts.query_scope(uow, platform.auth, run.run_id, {'service': 'app'})
    rules = {'users': {platform.auth.subject: ['logs.read']}, 'teams': {platform.auth.team_id: ['logs.read']},
        'agents': {platform.agent_id: ['logs.read']}, 'skills': {'__none__': ['logs.read']},
        'experts': {'diagnosis': ['logs.read']}}
    platform.backend._tool_permissions = DeploymentToolPermissions(lambda: rules)
    execution = await execution_for_run(platform, run)

    class Transport:
        calls = 0
        async def call(self, *args, **kwargs):
            self.calls += 1
            return {'observation': 'available'}

    class FakeModel:
        def bind_tools(self, tools): return self
        async def ainvoke(self, messages, **kwargs):
            from langchain_core.messages import ToolMessage
            if any(isinstance(m, ToolMessage) for m in messages):
                return AIMessage(content='Inspection finished.', usage_metadata={'input_tokens': 8, 'output_tokens': 3, 'total_tokens': 11})
            return AIMessage(content='', tool_calls=[{'name': 'logs_search', 'args': {'service': 'app'}, 'id': 'tool-' + str(n), 'type': 'tool_call'} for n in range(n_calls)],
                usage_metadata={'input_tokens': 8, 'output_tokens': 3, 'total_tokens': 11})

    class Factory:
        def build(self, *args, **kwargs): return FakeModel()

    transport = Transport()
    invoker = ModelInvoker(platform.backend, Factory(), object(), execution=execution)
    tools = PolicyToolExecutor(platform.backend, transport, execution=execution, delays=(0, 0))
    provider = InvocationGuard()
    saver = PortableCheckpointSaver(platform.backend, provider, CheckpointCodec())
    expert = DiagnosisExpert(execution, invoker, tools, [(ref, binding)], saver=saver, guard_provider=provider)
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


@pytest.mark.asyncio
@pytest.mark.parametrize("crash_point", ["none", "after_expert", "after_model", "after_model_scope_change", "after_tool", "after_tool_revoke", "after_tool_scope_change", "receipt_commit_fail"])
async def test_rca_graph_runs_configured_diagnosis_and_retains_scoped_evidence(platform, monkeypatch, crash_point):
    from workbench_agent.jobs.worker import Worker
    from workbench_agent.runtime.graphs.registry import foundation_worker_registry
    from workbench_agent.tools.permissions import DeploymentToolPermissions
    from langchain_core.messages import ToolMessage

    binding = ToolBinding(alias='logs.search', provider='mcp', tool_name='local.logs', server='logs',
        credential_scope='reader', permission='logs.read', input_schema={'type': 'object',
        'properties': {'service': {'type': 'string'}}, 'required': ['service'], 'additionalProperties': False})
    ref = await ToolRegistry(platform.backend).publish(platform.auth, platform.agent_id, binding, key='graph-tool')
    context = await platform.contexts.resolve(platform.auth, platform.agent_id)
    agent = await platform.profiles.get_agent(platform.auth, platform.agent_id)
    changed = await platform.profiles.save_draft(platform.auth, platform.agent_id,
        ProfileDraft(default_model=context.default_model, graph_version='dod.v4', tools=(ref,)),
        agent['revision'], key='graph-profile')
    await platform.profiles.publish(platform.auth, platform.agent_id, changed['revision'], key='graph-publish')
    run = await platform.create_run(mode='rca', summary_token_reserve=8000)
    async with platform.backend.create_uow().begin_write() as uow:
        await platform.contexts.query_scope(uow, platform.auth, run.run_id, {'service': 'app'})
    rules = {'users': {platform.auth.subject: ['logs.read']}, 'teams': {platform.auth.team_id: ['logs.read']},
        'agents': {platform.agent_id: ['logs.read']}, 'skills': {'__none__': ['logs.read']},
        'experts': {'diagnosis': ['logs.read']}}
    platform.backend._tool_permissions = DeploymentToolPermissions(lambda: rules)

    class Transport:
        async def call(self, *args, **kwargs): return {'observation': 'available'}
    class Model:
        def bind_tools(self, tools): return self
        async def ainvoke(self, messages, **kwargs):
            usage = {'input_tokens': 8, 'output_tokens': 3, 'total_tokens': 11}
            if any(isinstance(m, ToolMessage) for m in messages):
                return AIMessage(content='Inspection finished.', usage_metadata=usage)
            return AIMessage(content='', tool_calls=[{'name': 'logs_search', 'args': {'service': 'app'},
                'id': 'tool-1', 'type': 'tool_call'}], usage_metadata=usage)
    class Factory:
        def build(self, *args, **kwargs): return Model()
    def configure(execution):
        return (ModelInvoker(platform.backend, Factory(), object(), execution=execution),
                PolicyToolExecutor(platform.backend, Transport(), execution=execution, delays=(0, 0)))
    platform.backend._diagnosis_factory = configure
    registry = foundation_worker_registry(platform.backend)
    worker = Worker(platform.backend, 'graph-worker', registry)
    if crash_point != 'none':
        from workbench_agent.runtime.experts.diagnosis import DiagnosisExpert, BudgetedChatModel
        target, method = {'after_expert': (DiagnosisExpert, 'invoke'),
                          'after_model': (BudgetedChatModel, '_agenerate'),
                          'after_model_scope_change': (BudgetedChatModel, '_agenerate'),
                          'after_tool': (PolicyToolExecutor, 'execute'),
                          'after_tool_revoke': (PolicyToolExecutor, 'execute'),
                          'after_tool_scope_change': (PolicyToolExecutor, 'execute'),
                          'receipt_commit_fail': (platform.backend.tools, 'complete_invocation')}[crash_point]
        original = getattr(target, method)
        async def crash_after(*args, **kwargs):
            if crash_point != 'receipt_commit_fail':
                await original(*args, **kwargs)
            raise RuntimeError('simulated crash before checkpoint')
        monkeypatch.setattr(target, method, crash_after)
        assert await worker.run_once() == 'recovery_scheduled'
        monkeypatch.setattr(target, method, original)
        if crash_point == 'after_tool_revoke':
            rules['experts']['diagnosis'] = []
        if crash_point in ('after_tool_scope_change', 'after_model_scope_change'):
            async with platform.backend.create_uow().begin_write() as uow:
                await platform.contexts.query_scope(uow, platform.auth, run.run_id, {'service': 'other'})
        assert (await registry.reconciler.recover(run.run_id)).action == 'continue'
    assert await worker.run_once() == 'projected'
    result = await platform.get_run(run.run_id)
    assert result.status == 'partial'
    usage = await platform.budgets.snapshot(platform.auth, run.run_id)
    if crash_point == 'after_model':
        assert not result.outcome['evidence_refs']
        assert 'response could not be recovered' in result.outcome['unresolved_items'][0]
        assert usage.model_attempts == 1 and usage.tool_attempts == 0
    elif crash_point == 'after_model_scope_change':
        assert not result.outcome['evidence_refs']
        assert 'scope changed' in result.outcome['unresolved_items'][0].lower()
        assert usage.model_attempts == 1 and usage.tool_attempts == 0
    elif crash_point in ('after_tool_revoke', 'after_tool_scope_change', 'receipt_commit_fail'):
        assert not result.outcome['evidence_refs']
        if crash_point == 'receipt_commit_fail': assert await platform.count_rows('evidence') == 0
        assert usage.model_attempts == 2 and usage.tool_attempts == 1
    else:
        assert len(result.outcome['evidence_refs']) == 1
        assert 'Scoped diagnosis evidence' in result.outcome['summary']
        assert usage.model_attempts == 2 and usage.tool_attempts == 1

@pytest.mark.asyncio
@pytest.mark.parametrize('change',['none','revoke','scope'])
async def test_empty_mcp_result_survives_worker_crash_before_tool_checkpoint(platform,monkeypatch,change):
    from types import SimpleNamespace
    from langchain_core.messages import ToolMessage
    from workbench_agent.jobs.worker import Worker
    from workbench_agent.runtime.graphs.registry import foundation_worker_registry
    from workbench_agent.tools.permissions import DeploymentToolPermissions
    from workbench_agent.tools.mcp import MCPAdapter,ConnectionPolicy,TrustedConnection
    from contract.test_dod_mcp_mapping import SCOPE,GRANT

    binding=ToolBinding(alias='logs.search',provider='mcp',tool_name='logs_search',server='local',credential_scope='read',permission='logs.read',input_schema={'type':'object'})
    ref=await ToolRegistry(platform.backend).publish(platform.auth,platform.agent_id,binding,key='empty-tool')
    context=await platform.contexts.resolve(platform.auth,platform.agent_id);ws=await platform.profiles.get_agent(platform.auth,platform.agent_id)
    changed=await platform.profiles.save_draft(platform.auth,platform.agent_id,ProfileDraft(default_model=context.default_model,graph_version='dod.v4',tools=(ref,)),ws['revision'],key='empty-draft')
    await platform.profiles.publish(platform.auth,platform.agent_id,changed['revision'],key='empty-profile')
    run=await platform.create_run(mode='rca',summary_token_reserve=8000)
    async with platform.backend.create_uow().begin_write() as uow: await platform.contexts.query_scope(uow,platform.auth,run.run_id,SCOPE)
    rules={'users':{platform.auth.subject:['logs.read']},'teams':{platform.auth.team_id:['logs.read']},'agents':{platform.agent_id:['logs.read']},'skills':{'__none__':['logs.read']},'experts':{'diagnosis':['logs.read']}}
    platform.backend._tool_permissions=DeploymentToolPermissions(lambda:rules)
    class Transport(MCPAdapter):
        calls=0
        async def call(self,*args,**kwargs):
            self.calls+=1
            return SimpleNamespace(isError=False,structuredContent={'schema_version':'dod-tool-result-v1','status':'empty','data':{'entries':[]},'error':None,'retryable':False,'meta':{},'evidence_refs':[],'artifacts':[]})
    transport=Transport(None,ConnectionPolicy([TrustedConnection(context.team_id,context.agent_id,'local','read',frozenset({'logs_search'}),(GRANT,))]))
    seen=[]
    class Model:
        def bind_tools(self,tools): return self
        async def ainvoke(self,messages,**kwargs):
            usage={'input_tokens':8,'output_tokens':3,'total_tokens':11}
            tool_messages=[m for m in messages if isinstance(m,ToolMessage)]
            if tool_messages:
                seen.append(tool_messages[-1].content)
                return AIMessage(content='Done',usage_metadata=usage)
            return AIMessage(content='',tool_calls=[{'name':'logs_search','args':{'keyword':'error'},'id':'tool-1','type':'tool_call'}],usage_metadata=usage)
    class Factory:
        def build(self,*args,**kwargs): return Model()
    def configure(execution): return ModelInvoker(platform.backend,Factory(),object(),execution=execution),PolicyToolExecutor(platform.backend,transport,execution=execution,delays=(0,0))
    platform.backend._diagnosis_factory=configure
    registry=foundation_worker_registry(platform.backend);worker=Worker(platform.backend,'empty-worker',registry)
    original=PolicyToolExecutor.execute
    async def crash_after(*args,**kwargs):
        result=await original(*args,**kwargs)
        assert result.status=='empty'
        raise RuntimeError('simulated crash before tool checkpoint')
    monkeypatch.setattr(PolicyToolExecutor,'execute',crash_after)
    assert await worker.run_once()=='recovery_scheduled'
    monkeypatch.setattr(PolicyToolExecutor,'execute',original)
    if change=='revoke': rules['experts']['diagnosis']=[]
    if change=='scope':
        async with platform.backend.create_uow().begin_write() as uow:
            await platform.contexts.query_scope(uow,platform.auth,run.run_id,dict(SCOPE,region='other'))
    assert (await registry.reconciler.recover(run.run_id)).action=='continue'
    assert await worker.run_once()=='projected'
    assert transport.calls==1 and seen
    assert ('"status":"empty"' in seen[-1]) == (change=='none')
    usage=await platform.budgets.snapshot(platform.auth,run.run_id)
    assert usage.tool_attempts==1

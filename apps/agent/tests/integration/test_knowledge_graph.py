from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from workbench_agent.domain.profiles import ProfileDraft
from workbench_agent.domain.tools import ToolBinding
from workbench_agent.jobs.worker import Worker
from workbench_agent.runtime.graphs.registry import foundation_worker_registry
from workbench_agent.runtime.models.invoker import ModelInvoker
from workbench_agent.tools.permissions import DeploymentToolPermissions
from workbench_agent.tools.policy import PolicyToolExecutor
from workbench_agent.tools.registry import ToolRegistry


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["ok", "revoked", "budget", "tool_crash", "model_crash", "scope_change"] )
async def test_qa_worker_uses_knowledge_authority_and_preserves_evidence(platform, monkeypatch, scenario):
    from pathlib import Path
    import httpx
    from workbench_agent.tools.mcp import MCPAdapter, ConnectionPolicy, TrustedConnection, StreamableSessionFactory
    from contract.test_dod_mcp_mapping import GRANT
    from support.mcp import mcp_server_source
    server_src = mcp_server_source()
    monkeypatch.syspath_prepend(str(server_src))
    from workbench_mcp_server.bootstrap import Runtime
    from workbench_mcp_server.infrastructure.config.settings import Settings
    from workbench_mcp_server.interfaces.mcp.server import create_http_app
    monkeypatch.setenv('KNOWLEDGE_TEST_AGENT','fixture-agent')
    monkeypatch.setenv('KNOWLEDGE_TEST_UPSTREAM','fixture-upstream')
    scope={'team':'ExampleTeam','environment':'test','region':'id','service':'checkout','source_id':'kb'}
    settings=Settings.model_validate({'server':{'allowed_hosts':['localhost:*']},'principals':[{'id':'agent','token_secret_ref':'env://KNOWLEDGE_TEST_AGENT','tools':['knowledge_search'],'scopes':[scope]}],'sources':[{'id':'kb','kind':'knowledge','adapter':'normalized-http-v1','endpoint':'https://example.invalid/query','auth_secret_ref':'env://KNOWLEDGE_TEST_UPSTREAM','binding_id':'fixture-kb','scopes':[scope],'scope_enforced':True}]})
    runtime=Runtime(settings)
    class Gateway:
        calls=0
        async def query(self,request):
            self.calls+=1
            return {'matches':[{'source_id':'guide-1','title':'Guide','content':'Check the queue','source_uri':None}]}
    gateway=Gateway();runtime.executor.gateways['kb']=gateway
    app=create_http_app(runtime)
    lifespan=app.app.router.lifespan_context(app.app)
    await lifespan.__aenter__()
    factory=StreamableSessionFactory({('local','read'):'https://localhost:8000/mcp'},lambda *_:{'Authorization':'Bearer fixture-agent'},transport=httpx.ASGITransport(app=app))
    adapter=MCPAdapter(factory,ConnectionPolicy([TrustedConnection(platform.auth.team_id,platform.agent_id,'local','read',frozenset({'knowledge_search'}),(GRANT,))]))
    catalog=await adapter.discover('local','read',team_id=platform.auth.team_id,agent_id=platform.agent_id)
    schema=catalog.tools[catalog.names.index('knowledge_search')].inputSchema
    binding=ToolBinding(alias='kb.search',provider='mcp',tool_name='knowledge_search',server='local',credential_scope='read',permission='knowledge.read',input_schema=schema)
    ref=await ToolRegistry(platform.backend).publish(platform.auth,platform.agent_id,binding,key='knowledge-tool')
    context=await platform.contexts.resolve(platform.auth,platform.agent_id)
    ws=await platform.profiles.get_agent(platform.auth,platform.agent_id)
    draft=await platform.profiles.save_draft(platform.auth,platform.agent_id,ProfileDraft(default_model=context.default_model,graph_version='dod.v5',tools=(ref,)),ws['revision'],key='knowledge-draft')
    await platform.profiles.publish(platform.auth,platform.agent_id,draft['revision'],key='knowledge-profile')
    run=await platform.create_run(mode='qa',summary_token_reserve=8000,model_call_limit=1 if scenario=='budget' else None)
    from contract.test_dod_mcp_mapping import SCOPE
    async with platform.backend.create_uow().begin_write() as uow:
        await platform.contexts.query_scope(uow,platform.auth,run.run_id,SCOPE)
    rules={'users':{platform.auth.subject:['knowledge.read']},'teams':{platform.auth.team_id:['knowledge.read']},'agents':{platform.agent_id:['knowledge.read']},'skills':{'__none__':['knowledge.read']},'experts':{'knowledge':['knowledge.read']}}
    if scenario=='revoked': rules['experts']['knowledge']=[]
    platform.backend._tool_permissions=DeploymentToolPermissions(lambda:rules)
    class Model:
        def bind_tools(self,tools): return self
        async def ainvoke(self,messages,**kwargs):
            usage={'input_tokens':8,'output_tokens':3,'total_tokens':11}
            if any(isinstance(m,ToolMessage) for m in messages):
                return AIMessage(content='The guide recommends checking the queue.',usage_metadata=usage)
            return AIMessage(content='',tool_calls=[{'name':'kb_search','args':{'query':'queue','scene':'qa'},'id':'call-1','type':'tool_call'}],usage_metadata=usage)
    class Factory:
        def build(self,*args,**kwargs): return Model()
    transport=adapter
    def configure(execution):
        return ModelInvoker(platform.backend,Factory(),object(),execution=execution),PolicyToolExecutor(platform.backend,transport,execution=execution,delays=(0,0))
    platform.backend._diagnosis_factory=configure
    registry=foundation_worker_registry(platform.backend)
    worker=Worker(platform.backend,'knowledge-worker',registry)
    if scenario in ('tool_crash','model_crash','scope_change'):
        from workbench_agent.runtime.experts.knowledge import KnowledgeChatModel
        target,method=(PolicyToolExecutor,'execute') if scenario in ('tool_crash','scope_change') else (KnowledgeChatModel,'_agenerate')
        original=getattr(target,method)
        async def crash_after(*args,**kwargs):
            await original(*args,**kwargs)
            raise RuntimeError('simulated worker failure after external call')
        monkeypatch.setattr(target,method,crash_after)
        assert await worker.run_once()=='recovery_scheduled'
        monkeypatch.setattr(target,method,original)
        if scenario=='scope_change':
            async with platform.backend.create_uow().begin_write() as uow:
                await platform.contexts.query_scope(uow,platform.auth,run.run_id,dict(SCOPE,region='other'))
        assert (await registry.reconciler.recover(run.run_id)).action=='continue'
    assert await worker.run_once()=='projected'
    view=await platform.get_run(run.run_id)
    assert view.status=='partial'
    assert len(view.outcome['evidence_refs'])==(1 if scenario in ('ok','tool_crash') else 0)
    assert gateway.calls==(1 if scenario in ('ok','tool_crash','scope_change') else 0)
    if scenario in ('ok','tool_crash'):
        import json
        async with platform.backend.create_uow().begin_read() as uow:
            evidence=await platform.contexts.evidence_content(uow,platform.auth,run.run_id,str(view.outcome['evidence_refs'][0]))
        assert 'guide-1' in json.dumps(evidence)
        assert 'Check the queue' in json.dumps(evidence)
    await lifespan.__aexit__(None,None,None)
    await runtime.close()
    usage=await platform.budgets.snapshot(platform.auth,run.run_id)
    assert usage.model_attempts==(0 if scenario=='budget' else 1 if scenario=='model_crash' else 2)
    assert usage.tool_attempts==(1 if scenario in ('ok','tool_crash','scope_change') else 0)

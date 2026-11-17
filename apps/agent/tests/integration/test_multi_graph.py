import subprocess

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from workbench_agent.application.repository_identity import snapshot_identity_hash
from workbench_agent.domain.profiles import ProfileDraft, VersionRef
from workbench_agent.domain.tools import ToolBinding
from workbench_agent.jobs.worker import ExecutorRegistry, Worker
from workbench_agent.runtime.graphs.registry import foundation_worker_registry
from workbench_agent.runtime.models.invoker import ModelInvoker
from workbench_agent.skill_management.objects import LocalSkillObjects
from workbench_agent.tools.permissions import DeploymentToolPermissions
from workbench_agent.tools.policy import PolicyToolExecutor
from workbench_agent.tools.registry import ToolRegistry
from workbench_agent.execution.repositories import RepositoryService, RepositorySyncWorker


@pytest.mark.parametrize('scenario', ['ok', 'budget', 'knowledge_revoked', 'tool_limit', 'model_crash', 'tool_crash', 'code_model_crash', 'code_tool_crash'])
async def test_multi_experts_share_run_budget_and_scope(platform, tmp_path, scenario, monkeypatch):
    repo = tmp_path / 'repo'; repo.mkdir()
    def git(*args):
        return subprocess.check_output(['git', '-C', str(repo), *args], stderr=subprocess.DEVNULL).decode().strip()
    git('init', '-q'); git('config', 'user.name', 'Tester'); git('config', 'user.email', 'tester@example.invalid')
    (repo / 'main.py').write_text('answer = 42\n'); git('add', 'main.py'); git('commit', '-qm', 'first'); commit = git('rev-parse', 'HEAD')
    objects = LocalSkillObjects(tmp_path / 'objects')
    await RepositoryService(platform.backend).sync(platform.auth, platform.agent_id, 'app', commit, key='sync')
    reg = ExecutorRegistry(); reg.register_admin('repo_sync', RepositorySyncWorker(platform.backend, objects, {(platform.auth.team_id, platform.agent_id, 'app'): repo}).execute)
    assert await Worker(platform.backend, 'sync-worker', reg).run_admin_once() == 'admin_completed'
    async with platform.backend.create_uow().begin_read() as uow:
        row = dict(await platform.backend.configuration.one(uow, 'repo_snapshots', team_id=platform.auth.team_id, agent_id=platform.agent_id, repo_id='app', commit_sha=commit))
    repo_ref = VersionRef(resource_id=row['snapshot_id'], version=commit, content_hash=snapshot_identity_hash(row))
    from pathlib import Path
    import httpx
    from workbench_agent.tools.mcp import MCPAdapter, ConnectionPolicy, TrustedConnection, StreamableSessionFactory
    from contract.test_dod_mcp_mapping import GRANT, SCOPE
    from support.mcp import mcp_server_source
    server_src = mcp_server_source()
    monkeypatch.syspath_prepend(str(server_src))
    from workbench_mcp_server.bootstrap import Runtime
    from workbench_mcp_server.infrastructure.config.settings import Settings
    from workbench_mcp_server.interfaces.mcp.server import create_http_app
    monkeypatch.setenv('MULTI_TEST_AGENT','fixture-agent')
    monkeypatch.setenv('MULTI_TEST_UPSTREAM','fixture-upstream')
    scope={'team':'ExampleTeam','environment':'test','region':'id','service':'checkout','source_id':'kb'}
    settings=Settings.model_validate({'server':{'allowed_hosts':['localhost:*']},'principals':[{'id':'agent','token_secret_ref':'env://MULTI_TEST_AGENT','tools':['knowledge_search'],'scopes':[scope]}],'sources':[{'id':'kb','kind':'knowledge','adapter':'normalized-http-v1','endpoint':'https://example.invalid/query','auth_secret_ref':'env://MULTI_TEST_UPSTREAM','binding_id':'fixture-kb','scopes':[scope],'scope_enforced':True}]})
    runtime=Runtime(settings)
    class Gateway:
        calls=0
        async def query(self,request):
            self.calls+=1
            return {'matches':[{'source_id':'guide-1','title':'Guide','content':'Check the queue','source_uri':None}]}
    gateway=Gateway(); runtime.executor.gateways['kb']=gateway
    app=create_http_app(runtime); lifespan=app.app.router.lifespan_context(app.app); await lifespan.__aenter__()
    factory=StreamableSessionFactory({('local','read'):'https://localhost:8000/mcp'},lambda *_:{'Authorization':'Bearer fixture-agent'},transport=httpx.ASGITransport(app=app))
    transport=MCPAdapter(factory,ConnectionPolicy([TrustedConnection(platform.auth.team_id,platform.agent_id,'local','read',frozenset({'knowledge_search'}),(GRANT,))]))
    catalog=await transport.discover('local','read',team_id=platform.auth.team_id,agent_id=platform.agent_id)
    schema=catalog.tools[catalog.names.index('knowledge_search')].inputSchema
    binding=ToolBinding(alias='kb.search',provider='mcp',tool_name='knowledge_search',server='local',credential_scope='read',permission='knowledge.read',input_schema=schema)
    tool_ref=await ToolRegistry(platform.backend).publish(platform.auth,platform.agent_id,binding,key='tool')
    context=await platform.contexts.resolve(platform.auth,platform.agent_id)
    ws=await platform.profiles.get_agent(platform.auth,platform.agent_id)
    draft=await platform.profiles.save_draft(platform.auth,platform.agent_id,ProfileDraft(default_model=context.default_model,graph_version='dod.v7',tools=(tool_ref,),repositories=(repo_ref,)),ws['revision'],key='draft')
    await platform.profiles.publish(platform.auth,platform.agent_id,draft['revision'],key='profile')
    run=await platform.create_run(mode='code',summary_token_reserve=8000,model_call_limit=1 if scenario=='budget' else None)
    async with platform.backend.create_uow().begin_write() as uow:
        await platform.contexts.query_scope(uow,platform.auth,run.run_id,SCOPE)
    perms=['knowledge.read','repository.read']
    rules={'users':{platform.auth.subject:perms},'teams':{platform.auth.team_id:perms},'agents':{platform.agent_id:perms},'skills':{'__none__':perms},'experts':{'knowledge': [] if scenario=='knowledge_revoked' else perms,'code':perms}}
    platform.backend._tool_permissions=DeploymentToolPermissions(lambda:rules); platform.backend._snapshot_objects=objects
    class Model:
        def bind_tools(self,tools): self.tools=tools; return self
        async def ainvoke(self,messages,**kwargs):
            usage={'input_tokens':8,'output_tokens':3,'total_tokens':11}
            if any(isinstance(m,ToolMessage) for m in messages): return AIMessage(content='done',usage_metadata=usage)
            name=self.tools[0].name
            if name=='read_snapshot': calls=[{'name':name,'args':{'path':'main.py','start':1,'end':1},'id':'code-1','type':'tool_call'}]
            else: calls=[{'name':name,'args':{'query':'answer','scene':'qa'},'id':f'k-{i}','type':'tool_call'} for i in range(4 if scenario=='tool_limit' else 1)]
            return AIMessage(content='',tool_calls=calls,usage_metadata=usage)
    class Factory:
        def build(self,*args,**kwargs): return Model()
    platform.backend._diagnosis_factory=lambda execution:(ModelInvoker(platform.backend,Factory(),object(),execution=execution),PolicyToolExecutor(platform.backend,transport,execution=execution,delays=(0,0)))
    platform.backend._code_factory=lambda execution:ModelInvoker(platform.backend,Factory(),object(),execution=execution)
    registry=foundation_worker_registry(platform.backend)
    worker=Worker(platform.backend,'multi-worker',registry)
    if scenario in ('model_crash','tool_crash','code_model_crash','code_tool_crash'):
        from workbench_agent.runtime.experts.knowledge import KnowledgeChatModel
        from workbench_agent.runtime.experts.code import CodeChatModel
        from workbench_agent.tools.snapshot_reader import SnapshotReader
        target,method={'model_crash':(KnowledgeChatModel,'_agenerate'), 'tool_crash':(PolicyToolExecutor,'execute'),
                       'code_model_crash':(CodeChatModel,'_agenerate'), 'code_tool_crash':(SnapshotReader,'read')}[scenario]
        original=getattr(target,method)
        async def crash_after(*args,**kwargs):
            await original(*args,**kwargs)
            raise RuntimeError('failure before expert checkpoint')
        monkeypatch.setattr(target,method,crash_after)
        assert await worker.run_once()=='recovery_scheduled'
        monkeypatch.setattr(target,method,original)
        assert (await registry.reconciler.recover(run.run_id)).action=='continue'
    assert await worker.run_once()=='projected'
    view=await platform.get_run(run.run_id)
    assert view.status=='partial'
    usage=await platform.budgets.snapshot(platform.auth,run.run_id)
    if scenario=='budget': assert usage.model_attempts==0 and usage.tool_attempts==0
    elif scenario=='tool_limit': assert usage.tool_attempts==4 and gateway.calls==4
    elif scenario=='code_model_crash': assert usage.tool_attempts==1 and gateway.calls==1 and len(view.outcome['evidence_refs'])==1
    elif scenario=='model_crash': assert usage.tool_attempts==1 and gateway.calls==0 and len(view.outcome['evidence_refs'])==1
    elif scenario=='knowledge_revoked': assert usage.tool_attempts==1 and gateway.calls==0 and len(view.outcome['evidence_refs'])==1
    else: assert usage.tool_attempts==2 and gateway.calls==1 and len(view.outcome['evidence_refs'])==2

    await lifespan.__aexit__(None,None,None)
    await runtime.close()


async def test_only_live_v7_coordinator_can_issue_child_authority(platform):
    from support.skills import execution_for_run
    run=await platform.create_run(key='ordinary')
    parent=await execution_for_run(platform,run)
    with pytest.raises(PermissionError):
        with parent._tool_owner.expert_execution(parent, 'knowledge'):
            pass

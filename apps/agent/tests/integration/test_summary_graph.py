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


@pytest.mark.parametrize('scenario', ['ok', 'budget', 'invalid', 'summary_crash', 'scope_change', 'pre_summary_scope', 'permission_revoke', 'projection_revoke', 'projection_scope', 'recovery_revoke', 'recovery_scope', 'deadline_revoke', 'dispatch_revoke', 'dispatch_scope', 'alias_revoke', 'http_revoke', 'http_scope', 'http_legacy', 'http_reader', 'http_truncated', 'provider_error'])
async def test_summary_is_budgeted_and_extractive(platform, tmp_path, scenario, monkeypatch):
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
            return {'matches':[{'source_id':'guide-1','title':'Guide','content':('Long source content ' * 300 if scenario=='http_truncated' else 'Check the queue'),'source_uri':None}]}
    gateway=Gateway(); runtime.executor.gateways['kb']=gateway
    app=create_http_app(runtime); lifespan=app.app.router.lifespan_context(app.app); await lifespan.__aenter__()
    factory=StreamableSessionFactory({('local','read'):'https://localhost:8000/mcp'},lambda *_:{'Authorization':'Bearer fixture-agent'},transport=httpx.ASGITransport(app=app))
    transport=MCPAdapter(factory,ConnectionPolicy([TrustedConnection(platform.auth.team_id,platform.agent_id,'local','read',frozenset({'knowledge_search'}),(GRANT,))]))
    catalog=await transport.discover('local','read',team_id=platform.auth.team_id,agent_id=platform.agent_id)
    schema=catalog.tools[catalog.names.index('knowledge_search')].inputSchema
    binding=ToolBinding(alias='repository.read' if scenario=='alias_revoke' else 'kb.search',provider='mcp',tool_name='knowledge_search',server='local',credential_scope='read',permission='knowledge.read',input_schema=schema, max_output_bytes=512 if scenario=='http_truncated' else 65536)
    tool_ref=await ToolRegistry(platform.backend).publish(platform.auth,platform.agent_id,binding,key='tool')
    context=await platform.contexts.resolve(platform.auth,platform.agent_id)
    ws=await platform.profiles.get_agent(platform.auth,platform.agent_id)
    draft=await platform.profiles.save_draft(platform.auth,platform.agent_id,ProfileDraft(default_model=context.default_model,graph_id='knowledge.scoped_excerpt',tools=(tool_ref,),repositories=(repo_ref,)),ws['revision'],key='draft')
    await platform.profiles.publish(platform.auth,platform.agent_id,draft['revision'],key='profile')
    from workbench_agent.application.runs import RunService
    from workbench_agent.domain.runs import CreateRun
    request={'agent_id':platform.agent_id,'mode':'qa','input':{'text':'test'},'business_context':{'query_scope':SCOPE},'summary_token_reserve':8000}
    if scenario=='budget': request['budgets']={'model_calls':1}
    run=await RunService(platform.backend).create(platform.auth,CreateRun.model_validate(request),'summary-run')
    perms=['knowledge.read','repository.read']
    rules={'users':{platform.auth.subject:perms},'teams':{platform.auth.team_id:perms},'agents':{platform.agent_id:perms},'skills':{'__none__':perms},'experts':{'knowledge': [] if scenario=='knowledge_revoked' else perms,'code':perms}}
    platform.backend._tool_permissions=DeploymentToolPermissions(lambda:rules); platform.backend._snapshot_objects=objects
    class Model:
        def bind_tools(self,tools): self.tools=tools; return self
        async def ainvoke(self,messages,**kwargs):
            usage={'input_tokens':8,'output_tokens':3,'total_tokens':11}
            if kwargs.get('response_format'):
                if scenario == 'provider_error': raise RuntimeError('unavailable')
                import json
                from langchain_core.messages import HumanMessage
                data=json.loads(next(m.content for m in messages if isinstance(m, HumanMessage)))
                ref=next(iter(data['sources']))
                quote='fabricated' if scenario=='invalid' else data['sources'][ref][0]
                return AIMessage(content=json.dumps({'excerpts':[{'evidence_ref':ref,'quote':quote}]}),usage_metadata=usage)
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
    if scenario == 'pre_summary_scope':
        from workbench_agent.runtime.graphs.validation import EvidenceValidator
        original_validate=EvidenceValidator.validate
        def change_after_validate(*args, **kwargs):
            result=original_validate(*args, **kwargs)
            # Change is performed asynchronously just before summary reads evidence.
            platform.pending_scope_change=True
            return result
        monkeypatch.setattr(EvidenceValidator,'validate',change_after_validate)
        original_scope=platform.backend.runs.current_scope
        async def changed_scope(uow, team_id, run_id):
            if getattr(platform,'pending_scope_change',False):
                platform.pending_scope_change=False
                async with platform.backend.create_uow().begin_write() as change_uow:
                    await platform.contexts.query_scope(change_uow,platform.auth,run.run_id,{**SCOPE,'region':'sg'})
            return await original_scope(uow,team_id,run_id)
        monkeypatch.setattr(platform.backend.runs,'current_scope',changed_scope)
    if scenario in ('http_revoke','http_scope','http_legacy','http_reader'):
        from workbench_agent.runtime.results import ResultProjector
        original_http_project=ResultProjector.project
        async def add_private_delta(self,uow,claim,result):
            await platform.backend.execution_events.append_execution(uow,claim.guard,'assistant.delta',{'text':'SENSITIVE-FIXTURE-CONTENT'})
            return await original_http_project(self,uow,claim,result)
        monkeypatch.setattr(ResultProjector,'project',add_private_delta)
    if scenario in ('projection_revoke','projection_scope','recovery_revoke','recovery_scope','deadline_revoke'):

        from workbench_agent.runtime.results import ResultProjector
        original_project=ResultProjector.project
        async def before_project(self,uow,claim,result):
            if scenario in ('projection_revoke','recovery_revoke','deadline_revoke'):
                rules['experts']['knowledge']=[]; rules['experts']['code']=[]
            else:
                await platform.contexts.query_scope(uow,platform.auth,run.run_id,{**SCOPE,'region':'sg'})
            if scenario in ('recovery_revoke','recovery_scope','deadline_revoke'): raise RuntimeError('projection checkpoint crash')
            return await original_project(self,uow,claim,result)
        monkeypatch.setattr(ResultProjector,'project',before_project)
        if scenario in ('recovery_revoke','recovery_scope','deadline_revoke'):
            assert await worker.run_once()=='recovery_scheduled'
            monkeypatch.setattr(ResultProjector,'project',original_project)
            if scenario=='recovery_scope':
                async with platform.backend.create_uow().begin_write() as uow:
                    await platform.contexts.query_scope(uow,platform.auth,run.run_id,{**SCOPE,'region':'sg'})
            if scenario=='deadline_revoke':
                async with platform.backend.create_uow().begin_write() as uow:
                    candidates=await platform.backend.jobs.candidates(uow,run_id=run.run_id)
                    claim,_=await platform.backend.jobs.claim(uow,registry.capabilities(),'deadline-test',candidates,projection=True)
                    await platform.backend.runs.change(uow,'runs',{'team_id':platform.auth.team_id,'run_id':run.run_id},{'deadline_at':0})
                from workbench_agent.jobs.deadlines import DeadlineService
                assert await DeadlineService(platform.backend).expire_owned(claim.guard)
            else:
                assert (await registry.reconciler.recover(run.run_id)).action=='finalized'
            view=await platform.get_run(run.run_id)
            assert view.status=='partial' and not view.outcome['findings'] and not view.outcome['evidence_refs']
            assert 'scope or permissions changed' in view.outcome['summary']
            await lifespan.__aexit__(None,None,None); await runtime.close()
            return
    if scenario in ('dispatch_revoke','dispatch_scope','alias_revoke'):
        original_invoke=ModelInvoker.invoke
        async def before_dispatch(self,guard,role,*args,**kwargs):
            if role == 'summary':
                if scenario in ('dispatch_revoke','alias_revoke'):
                    rules['experts']['knowledge']=[]
                    if scenario=='dispatch_revoke': rules['experts']['code']=[]
                else:
                    async with platform.backend.create_uow().begin_write() as uow:
                        await platform.contexts.query_scope(uow,platform.auth,run.run_id,{**SCOPE,'region':'sg'})
            return await original_invoke(self,guard,role,*args,**kwargs)
        monkeypatch.setattr(ModelInvoker,'invoke',before_dispatch)
    if scenario in ('summary_crash', 'scope_change', 'permission_revoke'):

        original_invoke=ModelInvoker.invoke
        async def after_summary(self, guard, role, *args, **kwargs):
            result=await original_invoke(self, guard, role, *args, **kwargs)
            if role == 'summary':
                if scenario == 'summary_crash': raise RuntimeError('summary checkpoint lost')
                if scenario == 'permission_revoke':
                    rules['experts']['knowledge']=[]; rules['experts']['code']=[]
                    return result
                async with platform.backend.create_uow().begin_write() as uow:
                    await platform.contexts.query_scope(uow,platform.auth,run.run_id,{**SCOPE,'region':'sg'})
            return result
        monkeypatch.setattr(ModelInvoker,'invoke',after_summary)
        if scenario=='summary_crash':
            assert await worker.run_once()=='recovery_scheduled'
            monkeypatch.setattr(ModelInvoker,'invoke',original_invoke)
            assert (await registry.reconciler.recover(run.run_id)).action=='continue'
    assert await worker.run_once()=='projected'
    view=await platform.get_run(run.run_id)
    assert view.status=='partial'
    if scenario=='http_truncated':
        import httpx
        from workbench_agent.api.app import create_app
        async with platform.backend.create_uow().begin_read() as uow:
            evidence_rows=await platform.backend.runs.current_evidence(uow,platform.auth.team_id,run.run_id)
        truncated=[row for row in evidence_rows if row['quality']=='truncated']
        assert len(truncated)==1
        evidence_id=truncated[0]['evidence_id']
        assert evidence_id not in view.outcome['evidence_refs']
        # Audit access does not authorize presenting a truncated source as a finding.
        from workbench_agent.application.disclosure import project_outcome
        bogus_outcome={**view.outcome, 'evidence_refs':[evidence_id], 'findings':[{'summary':'partial source','status':'observed','evidence_refs':[evidence_id]}]}
        async with platform.backend.create_uow().begin_read() as uow:
            run_row=await platform.backend.runs.one(uow,'runs',team_id=platform.auth.team_id,run_id=run.run_id)
            projected=await project_outcome(platform.backend,uow,run_row,platform.auth,bogus_outcome)
        assert not projected['findings'] and not projected['evidence_refs']
        app_client=create_app(platform.settings,backend=platform.backend)
        async with app_client.router.lifespan_context(app_client):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_client),base_url='http://test',headers={'Authorization':f'Bearer development:{platform.auth.team_id}:{platform.auth.subject}'}) as client:
                response=await client.get(f'/v1/runs/{run.run_id}/evidence/{evidence_id}')
                assert response.status_code==200
                assert response.json()['truncated'] is True and response.json()['quality']=='truncated'
                async with platform.backend.create_uow().begin_read() as uow:
                    visible=await platform.contexts.current_evidence(uow,platform.auth,run.run_id)
                assert evidence_id in {row['evidence_id'] for row in visible}
                rules['experts']['knowledge']=[]
                assert (await client.get(f'/v1/runs/{run.run_id}/evidence/{evidence_id}')).status_code==404
                async with platform.backend.create_uow().begin_read() as uow:
                    visible=await platform.contexts.current_evidence(uow,platform.auth,run.run_id)
                assert evidence_id not in {row['evidence_id'] for row in visible}
                # Restoring a permission cannot make a forged receipt authoritative.
                rules['experts']['knowledge']=perms
                from sqlalchemy import update
                from workbench_agent.persistence import schema
                import json
                async with platform.backend.create_uow().begin_write() as uow:
                    invocations=await platform.backend.tools.succeeded_invocations(uow,platform.auth.team_id,run.run_id)
                    match=next(row for row in invocations if json.loads(row['receipt_ref']).get('evidence_id')==evidence_id)
                    forged={**json.loads(match['receipt_ref']), 'kind':'unknown-source-v1'}
                    await uow.execute(update(schema.tool_invocations).where(schema.tool_invocations.c.invocation_id==match['invocation_id']).values(receipt_ref=json.dumps(forged)))
                assert (await client.get(f'/v1/runs/{run.run_id}/evidence/{evidence_id}')).status_code==404
        await lifespan.__aexit__(None,None,None); await runtime.close()
        return
    if scenario in ('http_revoke','http_scope','http_legacy','http_reader'):
        import httpx, json
        from workbench_agent.api.app import create_app
        app_client=create_app(platform.settings,backend=platform.backend)
        async with app_client.router.lifespan_context(app_client):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_client),base_url='http://test',headers={'Authorization':f'Bearer development:{platform.auth.team_id}:{platform.auth.subject}'}) as client:
                ref=view.outcome['evidence_refs'][0]
                assert (await client.get(f'/v1/runs/{run.run_id}/evidence/{ref}')).status_code==200
                assert 'SENSITIVE-FIXTURE-CONTENT' in (await client.get(f'/v1/runs/{run.run_id}/events')).text
                async with platform.backend.create_uow().begin_read() as uow:
                    before=dict(await platform.backend.results.get(uow,platform.auth.team_id,run.run_id))
                queued=await app_client.state.events.read_batch(platform.auth,run.run_id,0)
                private_delta=next(event for event in queued if event.type=='assistant.delta')
                if scenario=='http_reader':
                    from uuid import uuid4
                    async with platform.backend.create_uow().begin_write() as uow:
                        now=await platform.backend.clock.now_ms(uow)
                        await platform.backend.configuration.insert(uow,'team_members',dict(member_id=str(uuid4()),team_id=platform.auth.team_id,subject='reader',role='viewer',status='active',created_at=now,updated_at=now))
                    client.headers['Authorization']=f'Bearer development:{platform.auth.team_id}:reader'
                elif scenario=='http_revoke':
                    rules['experts']['knowledge']=[]; rules['experts']['code']=[]
                elif scenario=='http_scope':
                    async with platform.backend.create_uow().begin_write() as uow:
                        await platform.contexts.query_scope(uow,platform.auth,run.run_id,{**SCOPE,'region':'sg'})
                else:
                    from sqlalchemy import update
                    from workbench_agent.persistence import schema
                    async with platform.backend.create_uow().begin_write() as uow:
                        await uow.execute(update(schema.tool_invocations).where(schema.tool_invocations.c.run_id==run.run_id).values(receipt_ref=None))
                if scenario!='http_reader':
                    projected=await app_client.state.events.project_event(platform.auth,private_delta)
                    assert 'SENSITIVE-FIXTURE-CONTENT' not in projected.payload.get('text','')
                assert (await client.get(f'/v1/runs/{run.run_id}/evidence/{ref}')).status_code==404
                response=(await client.get(f'/v1/runs/{run.run_id}')).json()
                assert not response['outcome']['evidence_refs'] and not response['outcome']['findings']
                messages=(await client.get(f'/v1/threads/{run.thread_id}/messages')).json()
                assert 'Source excerpt:' not in json.dumps(messages)
                assert 'SENSITIVE-FIXTURE-CONTENT' not in (await client.get(f'/v1/runs/{run.run_id}/events')).text
                if scenario=='http_reader':
                    rules['users']['reader']=perms
                    assert (await client.get(f'/v1/runs/{run.run_id}/evidence/{ref}')).status_code==200
                async with platform.backend.create_uow().begin_read() as uow:
                    after=dict(await platform.backend.results.get(uow,platform.auth.team_id,run.run_id))
                assert after['payload']==before['payload'] and after['digest']==before['digest']
        await lifespan.__aexit__(None,None,None); await runtime.close()
        return
    usage=await platform.budgets.snapshot(platform.auth,run.run_id)
    if scenario=='budget':
        assert usage.model_attempts==0
        assert not view.outcome['findings']
    elif scenario in ('invalid', 'summary_crash', 'scope_change', 'pre_summary_scope', 'permission_revoke', 'projection_revoke', 'projection_scope', 'recovery_revoke', 'recovery_scope', 'deadline_revoke', 'dispatch_revoke', 'dispatch_scope', 'alias_revoke', 'http_revoke', 'http_scope', 'http_legacy', 'http_reader', 'http_truncated', 'provider_error'):
        assert usage.model_attempts == (2 if scenario in ('pre_summary_scope','dispatch_revoke','dispatch_scope','alias_revoke') else 3)
        assert not view.outcome['findings']
        if scenario in ('scope_change','pre_summary_scope','permission_revoke','projection_revoke','projection_scope','dispatch_revoke','dispatch_scope'): assert not view.outcome['evidence_refs']
        if scenario in ('projection_revoke','projection_scope'): assert 'scope or permissions changed' in view.outcome['summary']
        if scenario=='alias_revoke':
            # A repository-like alias must not keep knowledge evidence visible
            # after the actual knowledge permission is revoked.
            assert not view.outcome['evidence_refs']
    else:
        assert usage.model_attempts==3
        assert view.outcome['findings']
        assert view.outcome['findings'][0]['summary'] == 'Source excerpt: Check the queue'

    await lifespan.__aexit__(None,None,None)
    await runtime.close()

"""Local protocol integration: no enterprise system or credentials are used."""
import json
from pathlib import Path
from uuid import uuid4
import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize('scenario', ['ok','empty','unavailable','timeout','crash','scope_change','revoked','budget','mixed_team','grant_change','grant_at_send','grant_after_checkpoint','legacy_receipt','check_durable'])
async def test_v10_baseline_uses_one_scope_and_recovers_without_resend(platform,monkeypatch,scenario):
    import httpx
    from workbench_agent.domain.profiles import ProfileDraft
    from workbench_agent.domain.tools import ToolBinding
    from workbench_agent.domain.runs import CreateRun
    from workbench_agent.application.runs import RunService
    from workbench_agent.jobs.worker import Worker
    from workbench_agent.runtime.graphs.registry import foundation_worker_registry
    from workbench_agent.tools.permissions import DeploymentToolPermissions
    from workbench_agent.tools.policy import PolicyToolExecutor
    from workbench_agent.tools.registry import ToolRegistry
    from workbench_agent.tools.mcp import MCPAdapter,ConnectionPolicy,TrustedConnection,StreamableSessionFactory
    from contract.test_dod_mcp_mapping import GRANT
    from support.mcp import mcp_server_source
    server_src = mcp_server_source()
    monkeypatch.syspath_prepend(str(server_src))
    from workbench_mcp_server.bootstrap import Runtime
    from workbench_mcp_server.infrastructure.config.settings import Settings
    from workbench_mcp_server.interfaces.mcp.server import create_http_app
    from workbench_mcp_server.application.results import GatewayError
    monkeypatch.setenv('M3_MCP_AGENT','fixture-agent')
    monkeypatch.setenv('M3_MCP_UPSTREAM','fixture-upstream')
    monkeypatch.setenv('M3_MCP_CURSOR','c'*32)
    names=('metrics_query','logs_search','trace_get')
    kinds=('metrics','logs','trace')
    scopes=[{'team':'DP','environment':'test','region':'id','service':'checkout','source_id':kind} for kind in kinds]
    sources=[]
    for name,kind,scope in zip(names,kinds,scopes):
        source={'id':kind,'kind':kind,'adapter':'normalized-http-v1','endpoint':'https://example.invalid/query',
                'auth_secret_ref':'env://M3_MCP_UPSTREAM','binding_id':'fixture-'+kind,'scopes':[scope],'scope_enforced':True}
        if kind=='metrics': source['templates']={'error_rate':{}}
        sources.append(source)
    config={'server':{'allowed_hosts':['localhost:*']},'principals':[{'id':'agent','token_secret_ref':'env://M3_MCP_AGENT','tools':list(names),'scopes':scopes}],
            'sources':sources,'cursor_secret_ref':'env://M3_MCP_CURSOR'}
    runtime=Runtime(Settings.model_validate(config)); calls=[]
    records={
        'metrics': {'series':[{'labels':{'service':'checkout'},'points':[['2026-11-13T00:01:00Z',0.1]],'unit':'ratio','source_id':'metric-1'}]},
        'logs': {'entries':[{'timestamp':'2026-11-13T00:01:00Z','service':'checkout','level':'ERROR','message':'sample','record_id':'log-1'}]},
        'trace': {'trace_id':'abc','spans':[{'span_id':'span-1','service':'checkout','name':'purchase','error':True,'start':'2026-11-13T00:01:00Z','end':'2026-11-13T00:01:01Z'}]},
    }
    class Gateway:
        def __init__(self,kind): self.kind=kind
        async def query(self,request):
            calls.append((self.kind,request))
            if self.kind=='logs' and scenario in ('unavailable','timeout'):
                if scenario=='timeout': raise TimeoutError()
                raise GatewayError('unavailable', 'FIXTURE_UNAVAILABLE')
            if self.kind=='logs' and scenario=='empty': return {'entries':[]}
            return records[self.kind]
    for kind in kinds: runtime.executor.gateways[kind]=Gateway(kind)
    app=create_http_app(runtime)
    lifespan=app.app.router.lifespan_context(app.app)
    await lifespan.__aenter__()
    try:
        factory=StreamableSessionFactory({('local','read'):'https://localhost:8000/mcp'},lambda *_:{'Authorization':'Bearer fixture-agent'},transport=httpx.ASGITransport(app=app))
        other_grant = dict(GRANT, team='OTHER')
        team = other_grant if scenario=='mixed_team' else GRANT
        connection_rules = [
            TrustedConnection(platform.auth.team_id,platform.agent_id,'local','read',frozenset({'metrics_query','trace_get'}),(GRANT,)),
            TrustedConnection(platform.auth.team_id,platform.agent_id,'local','read',frozenset({'logs_search'}),(team,)),
        ]
        adapter=MCPAdapter(factory,ConnectionPolicy(lambda:connection_rules))
        catalog=await adapter.discover('local','read',team_id=platform.auth.team_id,agent_id=platform.agent_id)
        refs=[]
        for name in names:
            schema=catalog.tools[catalog.names.index(name)].inputSchema
            binding=ToolBinding(alias='local.'+name,provider='mcp',tool_name=name,server='local',credential_scope='read',permission='baseline.read',input_schema=schema)
            refs.append(await ToolRegistry(platform.backend).publish(platform.auth,platform.agent_id,binding,key='tool-'+name))
        context=await platform.contexts.resolve(platform.auth,platform.agent_id); ws=await platform.profiles.get_agent(platform.auth,platform.agent_id)
        draft=await platform.profiles.save_draft(platform.auth,platform.agent_id,ProfileDraft(default_model=context.default_model,graph_version='dod.v10',tools=tuple(refs)),ws['revision'],key='baseline-draft')
        await platform.profiles.publish(platform.auth,platform.agent_id,draft['revision'],key='baseline-profile')
        request=CreateRun.model_validate({'agent_id':platform.agent_id,'mode':'rca','input':{'text':'Investigate'},
            'business_context':{'region':'id','service':'checkout','environment':'test','anchor':'2026-11-13T00:10:00Z','window_seconds':600,'trace_id':'abc'},'summary_token_reserve':8000,
            'budgets': {'tool_calls':1} if scenario=='budget' else {}})
        run=await RunService(platform.backend).create(platform.auth,request,'baseline-run')
        rules={'users':{platform.auth.subject:['baseline.read']},'teams':{platform.auth.team_id:['baseline.read']},
               'agents':{platform.agent_id:['baseline.read']},'skills':{'__none__':['baseline.read']},'experts':{'diagnosis':['baseline.read']}}
        if scenario=='revoked': rules['experts']['diagnosis']=[]
        platform.backend._tool_permissions=DeploymentToolPermissions(lambda:rules)
        platform.backend._rca_metric_template='error_rate'
        platform.backend._baseline_factory=lambda execution: PolicyToolExecutor(platform.backend,adapter,execution=execution,delays=(0,0))
        registry=foundation_worker_registry(platform.backend); worker=Worker(platform.backend,'baseline-worker',registry)
        if scenario=='check_durable':
            original=PolicyToolExecutor.execute
            async def check_before_dispatch(self,*args,**kwargs):
                async with platform.backend.create_uow().begin_read() as uow:
                    checkpoint=await platform.backend.runs.checkpoint(uow,platform.auth.team_id,run.run_id)
                assert checkpoint['values']['baseline_authority'] == ['DP','test','id','checkout',
                    '2026-11-13T00:00:00+00:00','2026-11-13T00:10:00+00:00']
                return await original(self,*args,**kwargs)
            monkeypatch.setattr(PolicyToolExecutor,'execute',check_before_dispatch)
        if scenario=='grant_at_send':
            original_call=adapter.call
            async def changed_at_send(*args, **kwargs):
                if args[2]=='logs_search':
                    connection_rules[1]=TrustedConnection(platform.auth.team_id,platform.agent_id,'local','read',frozenset({'logs_search'}),(other_grant,))
                return await original_call(*args, **kwargs)
            monkeypatch.setattr(adapter, 'call', changed_at_send)
        if scenario=='grant_change':
            original=PolicyToolExecutor.execute; changed=False
            async def change_grant_after(self,*args,**kwargs):
                nonlocal changed
                result=await original(self,*args,**kwargs)
                if not changed:
                    changed=True
                    connection_rules[1]=TrustedConnection(platform.auth.team_id,platform.agent_id,'local','read',frozenset({'logs_search'}),(other_grant,))
                return result
            monkeypatch.setattr(PolicyToolExecutor,'execute',change_grant_after)
        if scenario=='scope_change':
            original=PolicyToolExecutor.execute; changed=False
            async def change_after(self,*args,**kwargs):
                nonlocal changed
                result=await original(self,*args,**kwargs)
                if not changed:
                    changed=True
                    from workbench_agent.application.context import ContextResolver
                    async with platform.backend.create_uow().begin_write() as uow:
                        await ContextResolver(platform.backend).query_scope(uow,platform.auth,run.run_id,
                            {'region':'id','service':'checkout','environment':'test',
                             'start_at':'2026-11-13T00:01:00+00:00','end_at':'2026-11-13T00:10:00+00:00'})
                return result
            monkeypatch.setattr(PolicyToolExecutor,'execute',change_after)
        if scenario in ('crash','grant_after_checkpoint','legacy_receipt'):
            original=PolicyToolExecutor.execute; triggered=False
            async def crash_after(self,*args,**kwargs):
                nonlocal triggered
                result=await original(self,*args,**kwargs)
                if not triggered:
                    triggered=True
                    if scenario=='legacy_receipt':
                        from sqlalchemy import update
                        from workbench_agent.persistence import schema as schema
                        async with platform.backend.create_uow().begin_write() as uow:
                            row=await platform.backend.tools.invocation(uow, platform.auth.team_id, run.run_id, args[2])
                            receipt=json.loads(row['receipt_ref'])
                            receipt.pop('business_scope', None)
                            await uow.execute(update(schema.tool_invocations).where(
                                schema.tool_invocations.c.invocation_id==row['invocation_id']).values(receipt_ref=json.dumps(receipt)))
                    raise RuntimeError('simulated crash after receipt')
                return result
            monkeypatch.setattr(PolicyToolExecutor,'execute',crash_after)
            assert await worker.run_once()=='recovery_scheduled'
            monkeypatch.setattr(PolicyToolExecutor,'execute',original)
            if scenario=='grant_after_checkpoint':
                connection_rules[1]=TrustedConnection(platform.auth.team_id,platform.agent_id,'local','read',frozenset({'logs_search'}),(other_grant,))
            assert (await registry.reconciler.recover(run.run_id)).action=='continue'
        assert await worker.run_once()=='projected'
        view=await platform.get_run(run.run_id)
        assert view.status=='partial'
        if scenario=='legacy_receipt':
            assert len(calls)==3 and len(view.outcome['evidence_refs'])==2
            assert any('metrics' in item and 'unknown' in item for item in view.outcome['unresolved_items'])
            async with platform.backend.create_uow().begin_read() as uow:
                visible=await platform.contexts.current_evidence(uow,platform.auth,run.run_id)
            assert len(visible)==2
            return
        if scenario in ('scope_change','budget','revoked','mixed_team','grant_change','grant_at_send','grant_after_checkpoint'):
            expected_calls = 0 if scenario in ('revoked','mixed_team') else 1
            assert len(calls)==expected_calls
            assert len(view.outcome['evidence_refs']) == (1 if scenario=='budget' else 0)
            usage=await platform.budgets.snapshot(platform.auth,run.run_id)
            assert usage.tool_attempts==(2 if scenario=='grant_at_send' else expected_calls)
            return
        assert len(view.outcome['evidence_refs']) == (3 if scenario in ('ok','crash','check_durable') else 2)
        assert not any(f['status']=='normal' for f in view.outcome['findings'])
        if scenario in ('empty', 'unavailable', 'timeout'):
            assert any('logs' in item and scenario in item for item in view.outcome['unresolved_items'])
        async with platform.backend.create_uow().begin_read() as uow:
            saved=await platform.backend.runs.checkpoint(uow,platform.auth.team_id,run.run_id)
            scope=await platform.backend.runs.current_scope(uow,platform.auth.team_id,run.run_id)
            evidence=await platform.backend.runs.current_evidence(uow,platform.auth.team_id,run.run_id)
        statuses=saved['values']['baseline_statuses']
        assert statuses['metrics']=='valid' and statuses['trace']=='valid'
        assert statuses['logs']=={'ok':'valid','empty':'empty','unavailable':'unavailable','timeout':'timeout','crash':'valid','check_durable':'valid'}[scenario]
        assert len([kind for kind,_ in calls if kind=='metrics'])==1
        assert len([kind for kind,_ in calls if kind=='trace'])==1
        for kind,call in calls:
            assert call.context['query_scope_ref']==scope['scope_id']
            assert call.scope.service=='checkout' and call.scope.region=='id' and call.scope.environment=='test'
            from datetime import datetime
            assert datetime.fromisoformat(call.parameters['start']) == datetime.fromisoformat('2026-11-13T00:00:00+00:00')
            assert datetime.fromisoformat(call.parameters['end']) == datetime.fromisoformat('2026-11-13T00:10:00+00:00')
        for row in evidence:
            assert row['scope_id']==scope['scope_id']
            assert json.loads(row['time_window']) == {'start_at':'2026-11-13T00:00:00+00:00','end_at':'2026-11-13T00:10:00+00:00'}
        async with platform.backend.create_uow().begin_read() as uow:
            disclosed = await platform.contexts.current_evidence(uow, platform.auth, run.run_id)
        assert len(disclosed) == len(evidence) and len(disclosed) >= 2
        rules['experts']['diagnosis'] = []
        async with platform.backend.create_uow().begin_read() as uow:
            assert await platform.contexts.current_evidence(uow, platform.auth, run.run_id) == []
        usage=await platform.budgets.snapshot(platform.auth,run.run_id)
        assert usage.tool_attempts==len(calls)
    finally:
        await lifespan.__aexit__(None,None,None)
        await runtime.close()

import pytest

SCOPE={'service':'checkout','environment':'test','region':'id','start_at':'2026-11-13 08:00:00','end_at':'2026-11-13 08:10:00'}
GRANT={'kind':'dod-mcp-v1','team':'ExampleTeam','allowed_scope':{'service':'checkout','environment':'test','region':'id'},'earliest':'2026-11-13 00:00:00','latest':'2026-11-14 00:00:00','max_span_seconds':3600,'timezone':'Asia/Shanghai','sources':{'knowledge_search':'kb','metrics_query':'metrics','logs_search':'logs','trace_get':'trace'}}
CONTEXT={'run_id':'run-1','invocation_id':'invoke-1','query_scope_ref':'scope-1'}

@pytest.mark.parametrize('name,args',[
    ('knowledge_search',{'query':'help','scene':'qa'}),
    ('metrics_query',{'template_id':'error_rate','parameters':{}}),
    ('logs_search',{'keyword':'error'}),
    ('trace_get',{'trace_id':'abc'}),
])
def test_local_mapping_binds_scope_and_context(name,args):
    from workbench_agent.tools.mcp import build_dod_arguments
    result=build_dod_arguments(name,args,SCOPE,GRANT,CONTEXT)
    assert result['scope']=={'team':'ExampleTeam','environment':'test','region':'id','service':'checkout','source_id':GRANT['sources'][name]}
    assert result['context']==CONTEXT
    if name!='knowledge_search':
        assert result['start']=='2026-11-13T08:00:00+08:00' and result['end']=='2026-11-13T08:10:00+08:00'

@pytest.mark.parametrize('override',[{'scope':{}},{'context':{}},{'start':'2020-01-01T00:00:00Z'},{'team':'evil'},{'source_id':'evil'}])
def test_local_mapping_rejects_model_scope_or_identity(override):
    from workbench_agent.tools.mcp import build_dod_arguments
    with pytest.raises(PermissionError): build_dod_arguments('logs_search',override,SCOPE,GRANT,CONTEXT)

def test_local_mapping_rejects_unauthorized_scope_and_long_window():
    from workbench_agent.tools.mcp import build_dod_arguments
    for scope in (dict(SCOPE,region='sg'),dict(SCOPE,end_at='2026-11-13 10:00:00'),dict(SCOPE,service='')):
        with pytest.raises(PermissionError): build_dod_arguments('logs_search',{},scope,GRANT,CONTEXT)

@pytest.mark.parametrize('provider_status,expected',[('empty','empty'),('denied','denied'),('timeout','timeout'),('unavailable','unavailable'),('invalid_input','invalid_input'),('unknown','unknown')])
async def test_server_status_is_not_misreported_as_success(platform,provider_status,expected):
    from types import SimpleNamespace
    from workbench_agent.domain.tools import ToolBinding
    from workbench_agent.domain.profiles import ProfileDraft
    from workbench_agent.tools.registry import ToolRegistry
    from support.jobs import claim
    from contract.test_tool_policy import executor_for
    from workbench_agent.tools.mcp import MCPAdapter,ConnectionPolicy,TrustedConnection
    b=ToolBinding(alias='kb.local',provider='mcp',tool_name='knowledge_search',server='local',credential_scope='read',permission='read',input_schema={'type':'object'})
    ref=await ToolRegistry(platform.backend).publish(platform.auth,platform.agent_id,b,key='p')
    ctx=await platform.contexts.resolve(platform.auth,platform.agent_id);ws=await platform.profiles.get_agent(platform.auth,platform.agent_id)
    draft=await platform.profiles.save_draft(platform.auth,platform.agent_id,ProfileDraft(default_model=ctx.default_model,tools=(ref,)),ws['revision'],key='d')
    await platform.profiles.publish(platform.auth,platform.agent_id,draft['revision'],key='pp')
    run=await platform.create_run(summary_token_reserve=8000)
    async with platform.backend.create_uow().begin_write() as uow: await platform.contexts.query_scope(uow,platform.auth,run.run_id,SCOPE)
    claimed=await claim(platform)
    class Adapter(MCPAdapter):
        async def call(self,*a,**k): return SimpleNamespace(isError=provider_status not in ('ok','empty'),structuredContent={'schema_version':'dod-tool-result-v1','status':provider_status,'data':{'matches':[]} if provider_status=='empty' else {},'error':{'code':'TEST'},'retryable':False,'meta':{},'evidence_refs':[],'artifacts':[]})
    adapter=Adapter(None,ConnectionPolicy([TrustedConnection(ctx.team_id,ctx.agent_id,'local','read',frozenset({'knowledge_search'}),(GRANT,))]))
    result=await executor_for(platform,claimed,adapter,'read',delays=(0,0)).execute(claimed.context,claimed.guard,'logical',ref,{'query':'help','scene':'qa'})
    assert result.status==expected and result.evidence_ref is None

@pytest.mark.parametrize('name,args,record_key,record',[
    ('knowledge_search',{'query':'help','scene':'qa'},'matches',{'source_id':'doc-1','title':'Guide','content':'Check metrics','source_uri':None}),
    ('metrics_query',{'template_id':'error_rate','parameters':{}},'series',{'labels':{'service':'checkout'},'points':[['2026-11-13T00:01:00Z',0.1]],'unit':'ratio','source_id':'metric-1'}),
    ('logs_search',{'keyword':'error'},'entries',{'timestamp':'2026-11-13T00:01:00Z','service':'checkout','level':'ERROR','message':'sample','record_id':'log-1'}),
    ('trace_get',{'trace_id':'abc'},'spans',{'span_id':'span-1','service':'checkout','name':'purchase','error':True,'start':'2026-11-13T00:01:00Z','end':'2026-11-13T00:01:01Z'}),
])
async def test_actual_workbench_mcp_server_end_to_end(platform,monkeypatch,name,args,record_key,record):
    from pathlib import Path
    import httpx
    from support.mcp import mcp_server_source
    server_src = mcp_server_source()
    monkeypatch.syspath_prepend(str(server_src))
    from workbench_mcp_server.bootstrap import Runtime
    from workbench_mcp_server.infrastructure.config.settings import Settings
    from workbench_mcp_server.interfaces.mcp.server import create_http_app
    from workbench_agent.domain.tools import ToolBinding
    from workbench_agent.domain.profiles import ProfileDraft
    from workbench_agent.tools.registry import ToolRegistry
    from support.jobs import claim
    from contract.test_tool_policy import executor_for
    from workbench_agent.tools.mcp import MCPAdapter,ConnectionPolicy,TrustedConnection,StreamableSessionFactory
    monkeypatch.setenv('LOCAL_MCP_TEST_AGENT','fixture-agent')
    monkeypatch.setenv('LOCAL_MCP_TEST_UPSTREAM','fixture-upstream')
    monkeypatch.setenv('LOCAL_MCP_TEST_CURSOR','a'*32)
    kinds={'knowledge_search':'knowledge','metrics_query':'metrics','logs_search':'logs','trace_get':'trace'}
    source_id=GRANT['sources'][name]
    resource={'team':'ExampleTeam','environment':'test','region':'id','service':'checkout','source_id':source_id}
    source={'id':source_id,'kind':kinds[name],'adapter':'normalized-http-v1','endpoint':'https://example.invalid/query','auth_secret_ref':'env://LOCAL_MCP_TEST_UPSTREAM','binding_id':'fixture-'+source_id,'scopes':[resource],'scope_enforced':True}
    if name=='metrics_query': source['templates']={'error_rate':{}}
    config={'server':{'allowed_hosts':['localhost:*']},'principals':[{'id':'agent','token_secret_ref':'env://LOCAL_MCP_TEST_AGENT','tools':[name],'scopes':[resource]}],'sources':[source]}
    if name=='logs_search': config['cursor_secret_ref']='env://LOCAL_MCP_TEST_CURSOR'
    runtime=Runtime(Settings.model_validate(config))
    calls=[]
    class Gateway:
        async def query(self,request):
            calls.append(request)
            return {record_key:[record]}
    runtime.executor.gateways[source_id]=Gateway()
    app=create_http_app(runtime)
    try:
        async with app.app.router.lifespan_context(app.app):
            factory=StreamableSessionFactory({('local','read'):'https://localhost:8000/mcp'},lambda *_:{'Authorization':'Bearer fixture-agent'},transport=httpx.ASGITransport(app=app))
            policy=ConnectionPolicy([TrustedConnection(platform.auth.team_id,platform.agent_id,'local','read',frozenset({name}),(GRANT,))])
            adapter=MCPAdapter(factory,policy)
            catalog=await adapter.discover('local','read',team_id=platform.auth.team_id,agent_id=platform.agent_id)
            tool=catalog.tools[catalog.names.index(name)]
            binding=ToolBinding(alias='local.'+name,provider='mcp',tool_name=name,server='local',credential_scope='read',permission='read',input_schema=tool.inputSchema)
            registry=ToolRegistry(platform.backend)
            if name=='knowledge_search':
                import json
                from workbench_agent.tools.probe import ToolProbe
                from workbench_agent.jobs.worker import Worker,ExecutorRegistry
                from workbench_agent.jobs.admin import AdminJobService
                from workbench_agent.domain.admin_jobs import AdminAuth
                await registry.save_draft(platform.auth,platform.agent_id,binding,expected_revision=0,key='probe-draft')
                job=await registry.enqueue_probe(platform.auth,platform.agent_id,binding.alias,key='probe-job')
                request={(platform.auth.team_id,platform.agent_id,'local','read',name):{'scope':SCOPE,'args':args}}
                workers=ExecutorRegistry();workers.register_admin('tool_probe',ToolProbe(platform.backend,adapter,probe_requests=request).execute)
                assert await Worker(platform.backend,'probe-worker',workers).run_admin_once()=='admin_completed'
                result=await AdminJobService(platform.backend).get(AdminAuth(platform.auth.team_id,platform.agent_id,platform.auth.subject,True),job.job_id)
                assert json.loads(result.result_ref)['invocation']=='ok'
                assert calls[0].context['run_id']=='probe-'+job.job_id
                calls.clear()
            ref=await registry.publish(platform.auth,platform.agent_id,binding,key='publish')
            ctx=await platform.contexts.resolve(platform.auth,platform.agent_id);ws=await platform.profiles.get_agent(platform.auth,platform.agent_id)
            draft=await platform.profiles.save_draft(platform.auth,platform.agent_id,ProfileDraft(default_model=ctx.default_model,tools=(ref,)),ws['revision'],key='draft')
            await platform.profiles.publish(platform.auth,platform.agent_id,draft['revision'],key='profile')
            run=await platform.create_run(summary_token_reserve=8000)
            async with platform.backend.create_uow().begin_write() as uow: scope=await platform.contexts.query_scope(uow,platform.auth,run.run_id,SCOPE)
            claimed=await claim(platform)
            result=await executor_for(platform,claimed,adapter,'read',delays=(0,0)).execute(claimed.context,claimed.guard,'logical-1',ref,args)
            assert result.status=='ok' and result.evidence_ref
            assert result.data['data'][record_key]
            assert len(calls)==1 and calls[0].scope.team=='ExampleTeam' and calls[0].scope.source_id==source_id
            assert calls[0].context=={'run_id':run.run_id,'invocation_id':'logical-1','query_scope_ref':scope['scope_id']}
            assert (await platform.budgets.snapshot(platform.auth,run.run_id)).tool_attempts==1
    finally:
        await runtime.close()

def test_tool_schema_allows_safe_local_defs_but_not_remote_or_recursive():
    from workbench_agent.tools.registry import validate_tool_schema
    validate_tool_schema({'type':'object','$defs':{'Scope':{'type':'string'}},'properties':{'scope':{'$ref':'#/$defs/Scope'}}})
    with pytest.raises(ValueError): validate_tool_schema({'$ref':'https://attacker.invalid/schema'})
    with pytest.raises(ValueError): validate_tool_schema({'$defs':{'Loop':{'$ref':'#/$defs/Loop'}},'$ref':'#/$defs/Loop'})
    with pytest.raises(ValueError): validate_tool_schema({'$defs':{'Scope':{'type':'string'}},'$ref':'#/$defs/Missing'})

async def test_forged_provider_evidence_scope_is_not_recorded(platform):
    from types import SimpleNamespace
    from workbench_agent.domain.tools import ToolBinding
    from workbench_agent.domain.profiles import ProfileDraft
    from workbench_agent.tools.registry import ToolRegistry
    from support.jobs import claim
    from contract.test_tool_policy import executor_for
    from workbench_agent.tools.mcp import MCPAdapter,ConnectionPolicy,TrustedConnection
    b=ToolBinding(alias='kb.local',provider='mcp',tool_name='knowledge_search',server='local',credential_scope='read',permission='read',input_schema={'type':'object'})
    ref=await ToolRegistry(platform.backend).publish(platform.auth,platform.agent_id,b,key='p')
    ctx=await platform.contexts.resolve(platform.auth,platform.agent_id);ws=await platform.profiles.get_agent(platform.auth,platform.agent_id)
    draft=await platform.profiles.save_draft(platform.auth,platform.agent_id,ProfileDraft(default_model=ctx.default_model,tools=(ref,)),ws['revision'],key='d')
    await platform.profiles.publish(platform.auth,platform.agent_id,draft['revision'],key='pp')
    run=await platform.create_run(summary_token_reserve=8000)
    async with platform.backend.create_uow().begin_write() as uow: await platform.contexts.query_scope(uow,platform.auth,run.run_id,SCOPE)
    claimed=await claim(platform)
    class Adapter(MCPAdapter):
        async def call(self,*a,**k): return SimpleNamespace(isError=False,structuredContent={'schema_version':'dod-tool-result-v1','status':'ok','data':{'matches':[{'content':'bad'}]},'error':None,'retryable':False,'meta':{},'evidence_refs':[{'scope':{'team':'other'},'invocation_id':'other','query_scope_ref':'other','quality_status':'valid'}],'artifacts':[]})
    adapter=Adapter(None,ConnectionPolicy([TrustedConnection(ctx.team_id,ctx.agent_id,'local','read',frozenset({'knowledge_search'}),(GRANT,))]))
    result=await executor_for(platform,claimed,adapter,'read',delays=(0,0)).execute(claimed.context,claimed.guard,'logical',ref,{'query':'help','scene':'qa'})
    assert result.status=='unknown' and result.evidence_ref is None

def test_http_mcp_requires_explicit_numeric_loopback_opt_in():
    from uuid import uuid4
    from workbench_agent.settings import MCPConnectionBinding
    args=dict(server='local',credential_scope='read',team_id=str(uuid4()),agent_id=str(uuid4()),endpoint='http://127.0.0.1:8000/mcp',secret_ref='env://MCP_TEST',read_tools=('knowledge_search',))
    with pytest.raises(ValueError): MCPConnectionBinding(**args)
    assert MCPConnectionBinding(**args,allow_loopback_http=True).allow_loopback_http
    for bad in ('http://example.test/mcp','http://localhost/mcp','http://127.0.0.1.attacker.test/mcp'):
        with pytest.raises(ValueError): MCPConnectionBinding(**dict(args,endpoint=bad),allow_loopback_http=True)

@pytest.mark.parametrize('name,required,forbidden',[
    ('knowledge_search','query','scope'),('metrics_query','template_id','parameters'),
    ('logs_search','keyword','start'),('trace_get','trace_id','context'),
])
def test_expert_model_sees_only_safe_local_mcp_inputs(name,required,forbidden):
    from workbench_agent.runtime.experts.diagnosis import model_tool_schema
    schema=model_tool_schema(name,{})
    assert required in schema['properties'] and forbidden not in schema['properties']
    assert schema['additionalProperties'] is False

async def test_retryable_provider_timeout_consumes_each_attempt(platform):
    from types import SimpleNamespace
    from workbench_agent.domain.tools import ToolBinding
    from workbench_agent.domain.profiles import ProfileDraft
    from workbench_agent.tools.registry import ToolRegistry
    from support.jobs import claim
    from contract.test_tool_policy import executor_for
    from workbench_agent.tools.mcp import MCPAdapter,ConnectionPolicy,TrustedConnection
    b=ToolBinding(alias='kb.local',provider='mcp',tool_name='knowledge_search',server='local',credential_scope='read',permission='read',input_schema={'type':'object'})
    ref=await ToolRegistry(platform.backend).publish(platform.auth,platform.agent_id,b,key='p')
    ctx=await platform.contexts.resolve(platform.auth,platform.agent_id);ws=await platform.profiles.get_agent(platform.auth,platform.agent_id)
    draft=await platform.profiles.save_draft(platform.auth,platform.agent_id,ProfileDraft(default_model=ctx.default_model,tools=(ref,)),ws['revision'],key='d')
    await platform.profiles.publish(platform.auth,platform.agent_id,draft['revision'],key='pp')
    run=await platform.create_run(summary_token_reserve=8000)
    async with platform.backend.create_uow().begin_write() as uow: await platform.contexts.query_scope(uow,platform.auth,run.run_id,SCOPE)
    claimed=await claim(platform)
    class Adapter(MCPAdapter):
        calls=0
        async def call(self,*a,**k):
            self.calls+=1
            return SimpleNamespace(isError=True,structuredContent={'schema_version':'dod-tool-result-v1','status':'timeout','data':{},'error':{'code':'TIMEOUT'},'retryable':True,'meta':{},'evidence_refs':[],'artifacts':[]})
    adapter=Adapter(None,ConnectionPolicy([TrustedConnection(ctx.team_id,ctx.agent_id,'local','read',frozenset({'knowledge_search'}),(GRANT,))]))
    result=await executor_for(platform,claimed,adapter,'read',delays=(0,0)).execute(claimed.context,claimed.guard,'logical',ref,{'query':'help','scene':'qa'})
    assert result.status=='timeout' and result.error.retryable and adapter.calls==3
    assert (await platform.budgets.snapshot(platform.auth,run.run_id)).tool_attempts==3


def test_local_mapping_accepts_fixed_utc_scope_with_equivalent_grant_window():
    from workbench_agent.tools.mcp import build_dod_arguments
    scope = dict(SCOPE, start_at='2026-11-13T00:00:00+00:00', end_at='2026-11-13T00:10:00+00:00')
    for name,args in [('metrics_query',{'template_id':'error_rate'}),('logs_search',{}),('trace_get',{'trace_id':'abc'})]:
        result = build_dod_arguments(name,args,scope,GRANT,CONTEXT)
        assert result['start'] == '2026-11-13T08:00:00+08:00'
        assert result['end'] == '2026-11-13T08:10:00+08:00'


def test_utc_scope_cannot_expand_grant_or_use_ambiguous_naive_iso():
    from workbench_agent.tools.mcp import build_dod_arguments
    for scope in (
        dict(SCOPE, start_at='2026-11-12T15:59:59+00:00', end_at='2026-11-12T16:10:00+00:00'),
        dict(SCOPE, start_at='2026-11-13T00:00:00', end_at='2026-11-13T00:10:00'),
    ):
        with pytest.raises(PermissionError):
            build_dod_arguments('logs_search',{},scope,GRANT,CONTEXT)


def test_legacy_mapping_hash_is_frozen_for_existing_bindings():
    from workbench_agent.application.idempotency import digest
    from workbench_agent.domain.tools import ToolBinding

    value = ToolBinding(
        alias='local.knowledge_search',
        provider='mcp',
        tool_name='knowledge_search',
        server='local',
        credential_scope='read',
        permission='read',
        input_schema={'type': 'object'},
        output_schema=None,
        read_only=True,
        timeout_seconds=10,
        max_output_bytes=65536,
    ).model_dump(mode='json')
    assert 'argument_mapping' not in value
    assert digest(value) == 'ff50b51f749d62a8b736667368d0cc19cde86cbb6486d7b529ac0c18ece1d5c8'

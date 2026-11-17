import pytest

def allow_permissions(platform,permission):
    from workbench_agent.tools.permissions import DeploymentToolPermissions
    rules={'users':{platform.auth.subject:[permission]},'teams':{platform.auth.team_id:[permission]},'agents':{platform.agent_id:[permission]},'skills':{'__none__':[permission]},'experts':{'diagnosis':[permission]}}
    return DeploymentToolPermissions(lambda:rules)


def executor_for(platform,claimed,transport,permission=None,*,permissions=None,delays=(0.5,2)):
    from workbench_agent.jobs.worker import Worker,ExecutorRegistry
    from workbench_agent.tools.policy import PolicyToolExecutor
    platform.backend._tool_permissions=permissions or allow_permissions(platform,permission)
    worker=Worker(platform.backend,claimed.guard.worker_id,ExecutorRegistry())
    worker._active_tool_claims[id(claimed)]=claimed  # Test harness only; production run_once sets this after claim.
    return PolicyToolExecutor(platform.backend,transport,execution=worker._bind_tool_execution(claimed),delays=delays)

def test_permission_is_strict_intersection():
    from workbench_agent.tools.policy import effective_permissions
    assert effective_permissions({'read','write'},{'read','write'},{'read'},{'read','write'},{'read'}) == {'read'}
    assert effective_permissions({'read'},{'read'},{'read'},set(),{'read'}) == set()

def test_tool_input_schema_rejects_identity_override():
    from workbench_agent.domain.tools import ToolBinding
    from workbench_agent.tools.policy import validate_arguments
    binding=ToolBinding(alias='logs.search',provider='mcp',tool_name='local.logs',server='logs',credential_scope='reader',permission='logs.read',input_schema={'type':'object','properties':{'query':{'type':'string'}},'required':['query'],'additionalProperties':False})
    assert validate_arguments(binding, {'query':'ok'})
    assert not validate_arguments(binding, {'query':'ok','team_id':'other'})

@pytest.mark.asyncio
async def test_timeout_consumes_budget_and_is_not_normal(platform):
    from workbench_agent.domain.tools import ToolBinding
    from workbench_agent.domain.profiles import ProfileDraft
    from workbench_agent.tools.registry import ToolRegistry
    from workbench_agent.tools.policy import PolicyToolExecutor
    from support.jobs import claim
    binding=ToolBinding(alias='logs.search',provider='mcp',tool_name='local.logs',server='logs',credential_scope='reader',permission='logs.read',timeout_seconds=0.01,input_schema={'type':'object','properties':{'service':{'type':'string'}},'required':['service'],'additionalProperties':False})
    ref=await ToolRegistry(platform.backend).publish(platform.auth,platform.agent_id,binding,key='publish')
    profile=await platform.contexts.resolve(platform.auth,platform.agent_id)
    agent=await platform.profiles.get_agent(platform.auth,platform.agent_id)
    updated=await platform.profiles.save_draft(platform.auth,platform.agent_id,ProfileDraft(default_model=profile.default_model,tools=(ref,)),agent['revision'],key='draft')
    await platform.profiles.publish(platform.auth,platform.agent_id,updated['revision'],key='profile')
    run=await platform.create_run(summary_token_reserve=8000)
    async with platform.backend.create_uow().begin_write() as uow:
        await platform.contexts.query_scope(uow,platform.auth,run.run_id,{'service':'payment-api'})
    claimed=await claim(platform)
    import asyncio
    class Transport:
        async def call(self,*args,**kwargs): await asyncio.sleep(0.1)
    executor=executor_for(platform,claimed,Transport(),'logs.read',delays=(0,0))
    result=await executor.execute(claimed.context,claimed.guard,'logical-1',ref,{'service':'payment-api'})
    assert result.status=='timeout' and result.data is None and result.error.retryable
    assert (await platform.budgets.snapshot(platform.auth,run.run_id)).tool_attempts==3

@pytest.mark.asyncio
async def test_success_records_scoped_evidence_and_never_replays_dispatch(platform):
    from workbench_agent.domain.tools import ToolBinding
    from workbench_agent.domain.profiles import ProfileDraft
    from workbench_agent.tools.registry import ToolRegistry
    from workbench_agent.tools.policy import PolicyToolExecutor
    from support.jobs import claim
    binding=ToolBinding(alias='logs.search',provider='mcp',tool_name='local.logs',server='logs',credential_scope='reader',permission='logs.read',input_schema={'type':'object','properties':{'service':{'type':'string'},'query':{'type':'string'}},'additionalProperties':False})
    ref=await ToolRegistry(platform.backend).publish(platform.auth,platform.agent_id,binding,key='publish')
    context=await platform.contexts.resolve(platform.auth,platform.agent_id)
    ws=await platform.profiles.get_agent(platform.auth,platform.agent_id)
    updated=await platform.profiles.save_draft(platform.auth,platform.agent_id,ProfileDraft(default_model=context.default_model,graph_version='dod.v4',tools=(ref,)),ws['revision'],key='draft')
    await platform.profiles.publish(platform.auth,platform.agent_id,updated['revision'],key='profile')
    run=await platform.create_run(summary_token_reserve=8000)
    async with platform.backend.create_uow().begin_write() as uow:
        scope=await platform.contexts.query_scope(uow,platform.auth,run.run_id,{'service':'payment-api'})
    claimed=await claim(platform)
    class Transport:
        calls=0
        async def call(self,*args,**kwargs): self.calls+=1; return {'value':12}
    transport=Transport()
    executor=executor_for(platform,claimed,transport,'logs.read')
    result=await executor.execute(claimed.context,claimed.guard,'logical-1',ref,{'service':'payment-api'})
    assert result.status=='ok' and result.evidence_ref
    async with platform.backend.create_uow().begin_read() as uow:
        visible=await platform.contexts.evidence_content(uow,platform.auth,run.run_id,result.evidence_ref)
        assert visible['content']=={'value':12} and visible['truncated'] is False
    async with platform.backend.create_uow().begin_read() as uow:
        evidence=await platform.contexts.current_evidence(uow,platform.auth,run.run_id)
    assert len(evidence)==1 and evidence[0]['scope_id']==scope['scope_id'] and evidence[0]['evidence_id']==result.evidence_ref
    from workbench_agent.persistence.backends.sqlite import SQLiteBackend
    replacement=SQLiteBackend(platform.settings.storage.database_url.get_secret_value())
    try:
        async with replacement.create_uow().begin_read() as uow:
            payload=await replacement.tools.evidence_payload(uow,platform.auth.team_id,run.run_id,result.evidence_ref)
        assert payload and '12' in payload['payload'] and not payload['truncated']
    finally: await replacement.close()
    result2=await executor.execute(claimed.context,claimed.guard,'logical-1',ref,{'service':'payment-api'})
    assert result2==result and transport.calls==1
    from workbench_agent.application.errors import DomainError
    with pytest.raises(DomainError) as error:
        await executor.execute(claimed.context,claimed.guard,'logical-1',ref,{'service':'payment-api','query':'changed'})
    assert error.value.code=='INVOCATION_CONFLICT'
    executor.permissions._provider=lambda:{}
    denied=await executor.execute(claimed.context,claimed.guard,'logical-1',ref,{'service':'payment-api'})
    assert denied.status=='denied' and denied.data is None and transport.calls==1

@pytest.mark.asyncio
async def test_missing_scope_never_reaches_transport(platform):
    from workbench_agent.domain.tools import ToolBinding
    from workbench_agent.domain.profiles import ProfileDraft
    from workbench_agent.tools.registry import ToolRegistry
    from workbench_agent.tools.policy import PolicyToolExecutor
    from support.jobs import claim
    b=ToolBinding(alias='logs.search',provider='mcp',tool_name='local.logs',server='logs',credential_scope='reader',permission='logs.read',input_schema={'type':'object'})
    ref=await ToolRegistry(platform.backend).publish(platform.auth,platform.agent_id,b,key='publish')
    ctx=await platform.contexts.resolve(platform.auth,platform.agent_id)
    ws=await platform.profiles.get_agent(platform.auth,platform.agent_id)
    change=await platform.profiles.save_draft(platform.auth,platform.agent_id,ProfileDraft(default_model=ctx.default_model,tools=(ref,)),ws['revision'],key='draft')
    await platform.profiles.publish(platform.auth,platform.agent_id,change['revision'],key='profile')
    await platform.create_run(summary_token_reserve=8000)
    claim_=await claim(platform)
    class Transport:
        calls=0
        async def call(self,*args,**kwargs): self.calls+=1
    transport=Transport()
    result=await executor_for(platform,claim_,transport,'logs.read').execute(claim_.context,claim_.guard,'call',ref,{})
    assert result.status=='denied' and result.error.code=='QUERY_SCOPE_REQUIRED' and transport.calls==0

def test_scoped_arguments_must_be_present_and_match():
    from workbench_agent.tools.policy import arguments_within_scope
    assert arguments_within_scope({'service':'payment-api'}, {'service':'payment-api'})
    assert not arguments_within_scope({}, {'service':'payment-api'})
    assert not arguments_within_scope({'service':'other'}, {'service':'payment-api'})

@pytest.mark.asyncio
async def test_deployment_denial_does_not_retry(platform):
    from workbench_agent.domain.tools import ToolBinding
    from workbench_agent.domain.profiles import ProfileDraft
    from workbench_agent.tools.registry import ToolRegistry
    from workbench_agent.tools.policy import PolicyToolExecutor
    from support.jobs import claim
    b=ToolBinding(alias='logs.search',provider='mcp',tool_name='local.logs',server='logs',credential_scope='reader',permission='logs.read',input_schema={'type':'object','properties':{'service':{'type':'string'}}})
    ref=await ToolRegistry(platform.backend).publish(platform.auth,platform.agent_id,b,key='publish')
    ctx=await platform.contexts.resolve(platform.auth,platform.agent_id)
    ws=await platform.profiles.get_agent(platform.auth,platform.agent_id)
    change=await platform.profiles.save_draft(platform.auth,platform.agent_id,ProfileDraft(default_model=ctx.default_model,tools=(ref,)),ws['revision'],key='draft')
    await platform.profiles.publish(platform.auth,platform.agent_id,change['revision'],key='profile')
    run=await platform.create_run(summary_token_reserve=8000)
    async with platform.backend.create_uow().begin_write() as uow:
        await platform.contexts.query_scope(uow,platform.auth,run.run_id,{'service':'payment-api'})
    claimed=await claim(platform)
    class Transport:
        calls=0
        async def call(self,*args,**kwargs): self.calls+=1; raise PermissionError()
    transport=Transport()
    result=await executor_for(platform,claimed,transport,'logs.read',delays=(0,0)).execute(claimed.context,claimed.guard,'call',ref,{'service':'payment-api'})
    assert result.status=='denied' and transport.calls==1

@pytest.mark.asyncio
async def test_transport_timeout_never_exceeds_run_budget(platform):
    import asyncio,time
    from workbench_agent.domain.tools import ToolBinding
    from workbench_agent.domain.profiles import ProfileDraft
    from workbench_agent.tools.registry import ToolRegistry
    from workbench_agent.tools.policy import PolicyToolExecutor
    from support.jobs import claim
    b=ToolBinding(alias='logs.search',provider='mcp',tool_name='local.logs',server='logs',credential_scope='reader',permission='logs.read',timeout_seconds=10,input_schema={'type':'object','properties':{'service':{'type':'string'}}})
    ref=await ToolRegistry(platform.backend).publish(platform.auth,platform.agent_id,b,key='publish')
    ctx=await platform.contexts.resolve(platform.auth,platform.agent_id)
    ws=await platform.profiles.get_agent(platform.auth,platform.agent_id)
    change=await platform.profiles.save_draft(platform.auth,platform.agent_id,ProfileDraft(default_model=ctx.default_model,tools=(ref,)),ws['revision'],key='draft')
    await platform.profiles.publish(platform.auth,platform.agent_id,change['revision'],key='profile')
    run=await platform.create_run(summary_token_reserve=8000,wall_seconds=1)
    async with platform.backend.create_uow().begin_write() as uow:
        await platform.contexts.query_scope(uow,platform.auth,run.run_id,{'service':'payment-api'})
    claimed=await claim(platform)
    class Transport:
        async def call(self,*args,**kwargs): await asyncio.sleep(3)
    start=time.monotonic()
    try:
        await executor_for(platform,claimed,Transport(),'logs.read',delays=(0,0)).execute(claimed.context,claimed.guard,'call',ref,{'service':'payment-api'})
    except Exception as error:
        assert getattr(error,'code',None) in ('BUDGET_EXCEEDED','STALE_EXECUTION')
    assert time.monotonic()-start<2

@pytest.mark.asyncio
async def test_tool_draft_requires_revision_and_publishes_immutable_snapshot(platform):
    from workbench_agent.domain.tools import ToolBinding
    from workbench_agent.tools.registry import ToolRegistry
    from workbench_agent.application.errors import DomainError
    b=ToolBinding(alias='logs.search',provider='mcp',tool_name='local.logs',server='logs',credential_scope='reader',permission='logs.read',input_schema={'type':'object'})
    registry=ToolRegistry(platform.backend)
    draft=await registry.save_draft(platform.auth,platform.agent_id,b,expected_revision=0,key='d1')
    assert draft['revision']==1
    ref=await registry.publish_draft(platform.auth,platform.agent_id,b.alias,expected_revision=1,key='p1')
    changed=b.model_copy(update={'timeout_seconds':20})
    assert (await registry.save_draft(platform.auth,platform.agent_id,changed,expected_revision=1,key='d2'))['revision']==2
    assert await registry.publish_draft(platform.auth,platform.agent_id,b.alias,expected_revision=1,key='p1')==ref
    with pytest.raises(DomainError,match='REVISION_CONFLICT'):
        await registry.publish_draft(platform.auth,platform.agent_id,b.alias,expected_revision=1,key='p2')
    async with platform.backend.create_uow().begin_read() as uow:
        row=await platform.backend.configuration.one(uow,'tool_bindings',binding_id=ref.resource_id)
    assert ToolBinding.model_validate_json(row['schema']).timeout_seconds==10

@pytest.mark.asyncio
async def test_tool_draft_and_publish_api(platform,client):
    binding={'alias':'logs.search','provider':'mcp','tool_name':'local.logs','server':'logs','credential_scope':'reader','permission':'logs.read','input_schema':{'type':'object'}}
    path=f'/v1/agents/{platform.agent_id}/tool-bindings/logs.search'
    response=await client.put(path,json=binding,headers={'If-Match':'0','Idempotency-Key':'draft'})
    assert response.status_code==200 and response.json()['revision']==1
    response=await client.post(path+'/publish',headers={'If-Match':'1','Idempotency-Key':'publish'})
    assert response.status_code==201 and response.json()['version']=='1'

@pytest.mark.asyncio
async def test_probe_reports_discovery_and_schema_separately(platform):
    from workbench_agent.domain.tools import ToolBinding
    from workbench_agent.tools.registry import ToolRegistry
    from workbench_agent.tools.mcp import ToolCatalog
    b=ToolBinding(alias='logs.search',provider='mcp',tool_name='local.logs',server='logs',credential_scope='reader',permission='logs.read',input_schema={'type':'object','properties':{'service':{'type':'string'}}})
    registry=ToolRegistry(platform.backend)
    await registry.save_draft(platform.auth,platform.agent_id,b,expected_revision=0,key='d1')
    class Adapter:
        async def discover(self,*args,**kwargs):
            return ToolCatalog(('local.logs',),({'name':'local.logs','inputSchema':{'type':'object','properties':{'service':{'type':'integer'}}}},))
    result=await registry.probe_draft(platform.auth,platform.agent_id,b.alias,Adapter())
    assert result['discovery']=='ok' and result['schema']=='mismatch' and result['invocation']=='not_run'

@pytest.mark.asyncio
async def test_probe_api_fails_closed_without_deployment_mcp(platform,client):
    path=f'/v1/agents/{platform.agent_id}/tool-bindings/logs.search'
    response=await client.post(path+'/probe',headers={'Idempotency-Key':'missing'})
    assert response.status_code==409 and response.json()['code']=='DRAFT_REQUIRED'

@pytest.mark.asyncio
async def test_executor_uses_mcp_structured_content_as_tool_output(platform):
    import httpx
    from mcp.server.fastmcp import FastMCP
    from workbench_agent.tools.mcp import MCPAdapter,ConnectionPolicy,TrustedConnection,StreamableSessionFactory
    from workbench_agent.tools.registry import ToolRegistry
    from workbench_agent.tools.policy import PolicyToolExecutor
    from workbench_agent.domain.tools import ToolBinding
    from workbench_agent.domain.profiles import ProfileDraft
    from support.jobs import claim
    server=FastMCP('local',stateless_http=True,json_response=True)
    @server.tool(name='read',structured_output=True)
    def read(service:str)->dict[str,str]: return {'answer':service}
    app=server.streamable_http_app()
    binding=ToolBinding(alias='knowledge.read',provider='mcp',tool_name='read',server='local',credential_scope='read',permission='knowledge.read',input_schema=server._tool_manager.list_tools()[0].parameters,output_schema={'type':'object','properties':{'answer':{'type':'string'}},'required':['answer']})
    ref=await ToolRegistry(platform.backend).publish(platform.auth,platform.agent_id,binding,key='publish')
    ctx=await platform.contexts.resolve(platform.auth,platform.agent_id); ws=await platform.profiles.get_agent(platform.auth,platform.agent_id)
    change=await platform.profiles.save_draft(platform.auth,platform.agent_id,ProfileDraft(default_model=ctx.default_model,tools=(ref,)),ws['revision'],key='draft')
    await platform.profiles.publish(platform.auth,platform.agent_id,change['revision'],key='profile')
    run=await platform.create_run(summary_token_reserve=8000)
    async with platform.backend.create_uow().begin_write() as uow:
        await platform.contexts.query_scope(uow,platform.auth,run.run_id,{'service':'payment-api'})
    claimed=await claim(platform)
    async with app.router.lifespan_context(app):
        factory=StreamableSessionFactory({('local','read'):'https://localhost:8000/mcp'},lambda *_:{},transport=httpx.ASGITransport(app=app))
        adapter=MCPAdapter(factory,ConnectionPolicy([TrustedConnection(claimed.context.team_id,claimed.context.agent_id,'local','read',frozenset({'read'}))]))
        result=await executor_for(platform,claimed,adapter,'knowledge.read').execute(claimed.context,claimed.guard,'logical',ref,{'service':'payment-api'})
    assert result.status=='ok' and result.data=={'answer':'payment-api'} and result.evidence_ref

@pytest.mark.asyncio
async def test_write_binding_only_returns_action_proposal_never_calls_transport(platform):
    from workbench_agent.domain.tools import ToolBinding
    from workbench_agent.domain.profiles import ProfileDraft
    from workbench_agent.tools.registry import ToolRegistry
    from workbench_agent.tools.policy import PolicyToolExecutor
    from support.jobs import claim
    b=ToolBinding(alias='ticket.create',provider='mcp',tool_name='CreateTicket',server='ticket',credential_scope='write',permission='ticket.propose',read_only=False,input_schema={'type':'object','properties':{'service':{'type':'string'}}})
    ref=await ToolRegistry(platform.backend).publish(platform.auth,platform.agent_id,b,key='publish')
    ctx=await platform.contexts.resolve(platform.auth,platform.agent_id);ws=await platform.profiles.get_agent(platform.auth,platform.agent_id)
    change=await platform.profiles.save_draft(platform.auth,platform.agent_id,ProfileDraft(default_model=ctx.default_model,tools=(ref,)),ws['revision'],key='draft')
    await platform.profiles.publish(platform.auth,platform.agent_id,change['revision'],key='profile')
    run=await platform.create_run(summary_token_reserve=8000)
    async with platform.backend.create_uow().begin_write() as uow:
        await platform.contexts.query_scope(uow,platform.auth,run.run_id,{'service':'payment-api'})
    claimed=await claim(platform)
    class Transport:
        calls=0
        async def call(self,*args,**kwargs): self.calls+=1
    transport=Transport()
    result=await executor_for(platform,claimed,transport,'ticket.propose').execute(claimed.context,claimed.guard,'logical',ref,{'service':'payment-api'})
    assert result.proposed_action.binding_ref==ref and result.proposed_action.args=={'service':'payment-api'} and transport.calls==0

@pytest.mark.parametrize('scope',[{}, {'service':''}, {'service':'   '},{'environment':''}])
def test_empty_query_scopes_never_authorize(scope):
    from workbench_agent.tools.policy import arguments_within_scope
    assert not arguments_within_scope(dict(scope),scope)

@pytest.mark.asyncio
async def test_probe_api_enqueues_without_resolving_secret_or_network(platform,client):
    binding={'alias':'logs.search','provider':'mcp','tool_name':'local.logs','server':'logs','credential_scope':'reader','permission':'logs.read','input_schema':{'type':'object'}}
    path=f'/v1/agents/{platform.agent_id}/tool-bindings/logs.search'
    draft=await client.put(path,json=binding,headers={'If-Match':'0','Idempotency-Key':'draft-job'})
    assert draft.status_code==200
    class Forbidden:
        async def discover(self,*a,**k): raise AssertionError('API must not connect')
    client._transport.app.state.tool_adapter=Forbidden()
    response=await client.post(path+'/probe',headers={'Idempotency-Key':'probe-job'})
    assert response.status_code==202 and response.json()['kind']=='tool_probe' and response.json()['status']=='queued'
    replay=await client.post(path+'/probe',headers={'Idempotency-Key':'probe-job'})
    assert replay.json()['job_id']==response.json()['job_id']

@pytest.mark.asyncio
async def test_tool_probe_worker_uses_frozen_draft_and_local_mcp(platform):
    import httpx,json
    from mcp.server.fastmcp import FastMCP
    from workbench_agent.tools.mcp import MCPAdapter,ConnectionPolicy,TrustedConnection,StreamableSessionFactory
    from workbench_agent.tools.probe import ToolProbe
    from workbench_agent.jobs.worker import Worker,ExecutorRegistry
    from workbench_agent.tools.registry import ToolRegistry
    from workbench_agent.domain.tools import ToolBinding
    from workbench_agent.domain.admin_jobs import AdminAuth
    from workbench_agent.jobs.admin import AdminJobService
    server=FastMCP('local',stateless_http=True,json_response=True)
    @server.tool(name='read',structured_output=True)
    def read(service:str)->dict[str,str]: return {'answer':service}
    app=server.streamable_http_app()
    b=ToolBinding(alias='local.read',provider='mcp',tool_name='read',server='local',credential_scope='read',permission='read',input_schema=server._tool_manager.list_tools()[0].parameters)
    registry=ToolRegistry(platform.backend)
    await registry.save_draft(platform.auth,platform.agent_id,b,expected_revision=0,key='draft')
    job=await registry.enqueue_probe(platform.auth,platform.agent_id,b.alias,key='probe')
    async with app.router.lifespan_context(app):
        factory=StreamableSessionFactory({('local','read'):'https://localhost:8000/mcp'},lambda *_:{},transport=httpx.ASGITransport(app=app))
        adapter=MCPAdapter(factory,ConnectionPolicy([TrustedConnection(platform.auth.team_id,platform.agent_id,'local','read',frozenset({'read'}))]))
        reg=ExecutorRegistry();reg.register_admin('tool_probe',ToolProbe(platform.backend,adapter).execute)
        assert await Worker(platform.backend,'worker',reg).run_admin_once()=='admin_completed'
    result=await AdminJobService(platform.backend).get(AdminAuth(platform.auth.team_id,platform.agent_id,platform.auth.subject,True),job.job_id)
    report=json.loads(result.result_ref)
    assert report['configuration']=='valid' and report['discovery']=='ok' and report['schema']=='match' and report['invocation']=='not_run'

@pytest.mark.asyncio
async def test_probe_worker_only_invokes_explicit_deployment_owned_fixed_request(platform):
    import httpx,json
    from mcp.server.fastmcp import FastMCP
    from workbench_agent.tools.mcp import MCPAdapter,ConnectionPolicy,TrustedConnection,StreamableSessionFactory
    from workbench_agent.tools.probe import ToolProbe
    from workbench_agent.jobs.worker import Worker,ExecutorRegistry
    from workbench_agent.tools.registry import ToolRegistry
    from workbench_agent.domain.tools import ToolBinding
    from workbench_agent.domain.admin_jobs import AdminAuth
    from workbench_agent.jobs.admin import AdminJobService
    server=FastMCP('local',stateless_http=True,json_response=True)
    @server.tool(name='read',structured_output=True)
    def read(service:str)->dict[str,str]: return {'answer':service}
    app=server.streamable_http_app()
    b=ToolBinding(alias='local.read',provider='mcp',tool_name='read',server='local',credential_scope='read',permission='read',input_schema=server._tool_manager.list_tools()[0].parameters)
    registry=ToolRegistry(platform.backend);await registry.save_draft(platform.auth,platform.agent_id,b,expected_revision=0,key='draft')
    job=await registry.enqueue_probe(platform.auth,platform.agent_id,b.alias,key='probe')
    async with app.router.lifespan_context(app):
        factory=StreamableSessionFactory({('local','read'):'https://localhost:8000/mcp'},lambda *_:{},transport=httpx.ASGITransport(app=app))
        adapter=MCPAdapter(factory,ConnectionPolicy([TrustedConnection(platform.auth.team_id,platform.agent_id,'local','read',frozenset({'read'}))]))
        fixed={(platform.auth.team_id,platform.agent_id,'local','read','read'):{'service':'fixture'}}
        reg=ExecutorRegistry();reg.register_admin('tool_probe',ToolProbe(platform.backend,adapter,probe_requests=fixed).execute)
        assert await Worker(platform.backend,'worker',reg).run_admin_once()=='admin_completed'
    result=await AdminJobService(platform.backend).get(AdminAuth(platform.auth.team_id,platform.agent_id,platform.auth.subject,True),job.job_id)
    report=json.loads(result.result_ref)
    assert report['configuration']=='valid' and report['schema']=='match' and report['invocation']=='ok'
    assert 'fixture' not in result.result_ref

def test_evidence_payload_redacts_secrets_and_marks_truncation():
    from workbench_agent.domain.evidence import safe_evidence_payload
    payload,truncated=safe_evidence_payload({'token':'secret-value','message':'x'*1000},128)
    assert truncated and 'secret-value' not in payload and len(payload.encode())<=128
    payload,truncated=safe_evidence_payload({'authorization':'Bearer secret','answer':'useful'},128)
    assert not truncated and 'Bearer secret' not in payload and 'useful' in payload

@pytest.mark.asyncio
async def test_evidence_payload_read_requires_current_tenant_and_scope(platform):
    from workbench_agent.application.errors import DomainError
    async with platform.backend.create_uow().begin_read() as uow:
        with pytest.raises(DomainError):
            await platform.contexts.evidence_content(uow,platform.other_auth,'00000000-0000-0000-0000-000000000001','00000000-0000-0000-0000-000000000002')

@pytest.mark.asyncio
async def test_log_scope_grant_builds_real_schema_request_for_local_mcp(platform):
    import httpx
    from mcp.server.fastmcp import FastMCP
    from workbench_agent.tools.mcp import MCPAdapter,ConnectionPolicy,TrustedConnection,StreamableSessionFactory
    from workbench_agent.tools.registry import ToolRegistry
    from workbench_agent.tools.policy import PolicyToolExecutor
    from workbench_agent.domain.tools import ToolBinding
    from workbench_agent.domain.profiles import ProfileDraft
    from support.jobs import claim
    received=[]
    server=FastMCP('local',stateless_http=True,json_response=True)
    @server.tool(name='SRE-Log-Query',structured_output=True)
    def search(query:str,team:str,env:str,region:str,service_cmdb_name:str,begin_time:str,end_time:str)->dict[str,str]:
        received.append({'team':team,'env':env,'region':region,'service':service_cmdb_name,'begin':begin_time,'end':end_time})
        return {'answer':'safe'}
    app=server.streamable_http_app()
    binding=ToolBinding(alias='logs.search',provider='mcp',tool_name='SRE-Log-Query',server='local',credential_scope='read',permission='logs.read',input_schema=server._tool_manager.list_tools()[0].parameters)
    ref=await ToolRegistry(platform.backend).publish(platform.auth,platform.agent_id,binding,key='publish')
    ctx=await platform.contexts.resolve(platform.auth,platform.agent_id);ws=await platform.profiles.get_agent(platform.auth,platform.agent_id)
    change=await platform.profiles.save_draft(platform.auth,platform.agent_id,ProfileDraft(default_model=ctx.default_model,tools=(ref,)),ws['revision'],key='draft')
    await platform.profiles.publish(platform.auth,platform.agent_id,change['revision'],key='profile')
    run=await platform.create_run(summary_token_reserve=8000)
    scope={'service':'svc.test','environment':'test','region':'sg','start_at':'2026-01-01 00:00:00','end_at':'2026-01-01 00:10:00'}
    async with platform.backend.create_uow().begin_write() as uow:
        await platform.contexts.query_scope(uow,platform.auth,run.run_id,scope)
    claimed=await claim(platform)
    async with app.router.lifespan_context(app):
        factory=StreamableSessionFactory({('local','read'):'https://localhost:8000/mcp'},lambda *_:{},transport=httpx.ASGITransport(app=app))
        policy=ConnectionPolicy([TrustedConnection(ctx.team_id,ctx.agent_id,'local','read',frozenset({'SRE-Log-Query'}),({'scope':scope,'team':'trusted-team'},))])
        result=await executor_for(platform,claimed,MCPAdapter(factory,policy),'logs.read').execute(claimed.context,claimed.guard,'log',ref,{'query':'ERROR'})
    assert result.status=='ok' and received==[{'team':'trusted-team','env':'test','region':'sg','service':'svc.test','begin':scope['start_at'],'end':scope['end_at']}]

@pytest.mark.parametrize('layer',['user','team','agent','skill','expert'])
def test_trusted_permission_policy_requires_every_layer(layer):
    from types import SimpleNamespace
    from workbench_agent.tools.permissions import DeploymentToolPermissions
    context=SimpleNamespace(subject='u',team_id='t',agent_id='w',skills=())
    rules={'users':{'u':['read']},'teams':{'t':['read']},'agents':{'w':['read']},'skills':{'__none__':['read']},'experts':{'diagnosis':['read']}}
    provider=DeploymentToolPermissions(lambda:rules)
    assert provider.allows(context,'read','diagnosis')
    rules[{'user':'users','team':'teams','agent':'agents','skill':'skills','expert':'experts'}[layer]].clear()
    assert not provider.allows(context,'read','diagnosis')

@pytest.mark.asyncio
@pytest.mark.parametrize('layer',['users','teams','agents','skills','experts'])
async def test_revoked_permission_layer_prevents_dispatch(platform,layer):
    from workbench_agent.domain.tools import ToolBinding
    from workbench_agent.domain.profiles import ProfileDraft
    from workbench_agent.tools.registry import ToolRegistry
    from workbench_agent.tools.policy import PolicyToolExecutor
    from support.jobs import claim
    b=ToolBinding(alias='local.read',provider='mcp',tool_name='local.read',server='local',credential_scope='read',permission='read',input_schema={'type':'object','properties':{'service':{'type':'string'}}})
    ref=await ToolRegistry(platform.backend).publish(platform.auth,platform.agent_id,b,key='publish')
    ctx=await platform.contexts.resolve(platform.auth,platform.agent_id);ws=await platform.profiles.get_agent(platform.auth,platform.agent_id)
    draft=await platform.profiles.save_draft(platform.auth,platform.agent_id,ProfileDraft(default_model=ctx.default_model,tools=(ref,)),ws['revision'],key='draft')
    await platform.profiles.publish(platform.auth,platform.agent_id,draft['revision'],key='profile')
    run=await platform.create_run(summary_token_reserve=8000)
    async with platform.backend.create_uow().begin_write() as uow:
        await platform.contexts.query_scope(uow,platform.auth,run.run_id,{'service':'svc'})
    claimed=await claim(platform)
    class Transport:
        calls=0
        async def call(self,*a,**k): self.calls+=1
    transport=Transport();permissions=allow_permissions(platform,'read');permissions._provider()[layer].clear()
    result=await executor_for(platform,claimed,transport,permissions=permissions).execute(claimed.context,claimed.guard,'logical',ref,{'service':'svc'})
    assert result.status=='denied' and transport.calls==0

def test_historical_small_output_limit_fails_closed_without_validation_exception():
    from workbench_agent.tools.policy import load_published_binding
    from workbench_agent.domain.tools import ToolBinding
    b=ToolBinding(alias='x',provider='mcp',tool_name='read',server='s',credential_scope='c',permission='read',input_schema={'type':'object'})
    data=b.model_dump(mode='json');data['max_output_bytes']=1
    assert load_published_binding(data) is None

@pytest.mark.asyncio
async def test_production_probe_uses_trusted_business_scope_and_real_mcp_schema(platform):
    import httpx,json
    from mcp.server.fastmcp import FastMCP
    from workbench_agent.tools.mcp import configured_adapter
    from workbench_agent.tools.probe import ToolProbe
    from workbench_agent.jobs.worker import Worker,ExecutorRegistry
    from workbench_agent.tools.registry import ToolRegistry
    from workbench_agent.domain.tools import ToolBinding
    from workbench_agent.settings import MCPConnectionBinding
    from workbench_agent.domain.admin_jobs import AdminAuth
    from workbench_agent.jobs.admin import AdminJobService
    calls=[];server=FastMCP('local',stateless_http=True,json_response=True)
    @server.tool(name='SRE-Log-Query',structured_output=True)
    def log(query:str,team:str,env:str,region:str,service_cmdb_name:str,begin_time:str,end_time:str)->dict[str,str]:
        calls.append((team,env,service_cmdb_name));return {'ok':'yes'}
    app=server.streamable_http_app(); scope={'service':'svc','environment':'test','region':'sg','start_at':'2026-01-01 00:00:00','end_at':'2026-01-01 00:10:00'}
    b=ToolBinding(alias='logs',provider='mcp',tool_name='SRE-Log-Query',server='local',credential_scope='read',permission='read',input_schema=server._tool_manager.list_tools()[0].parameters)
    connection=MCPConnectionBinding(server='local',credential_scope='read',team_id=platform.auth.team_id,agent_id=platform.agent_id,endpoint='https://localhost:8000/mcp',secret_ref='env://TEST_TOKEN',read_tools=('SRE-Log-Query',),scope_grants=({'scope':scope,'team':'trusted'},),probe_requests={'SRE-Log-Query':{'scope':scope,'args':{'query':'ERROR'}}})
    class Secret:
        def resolve(self,ref): return 'fixture'
    registry=ToolRegistry(platform.backend);await registry.save_draft(platform.auth,platform.agent_id,b,expected_revision=0,key='draft')
    job=await registry.enqueue_probe(platform.auth,platform.agent_id,b.alias,key='probe')
    async with app.router.lifespan_context(app):
        adapter=configured_adapter([connection],Secret());adapter.session_factory.transport=httpx.ASGITransport(app=app)
        fixed={(connection.team_id,connection.agent_id,connection.server,connection.credential_scope,'SRE-Log-Query'):connection.probe_requests['SRE-Log-Query']}
        reg=ExecutorRegistry();reg.register_admin('tool_probe',ToolProbe(platform.backend,adapter,probe_requests=fixed).execute)
        assert await Worker(platform.backend,'worker',reg).run_admin_once()=='admin_completed'
    result=await AdminJobService(platform.backend).get(AdminAuth(platform.auth.team_id,platform.agent_id,platform.auth.subject,True),job.job_id)
    assert json.loads(result.result_ref)['invocation']=='ok' and calls==[('trusted','test','svc')]

def test_tool_executor_rejects_unbound_or_forged_authority(platform):
    from workbench_agent.tools.policy import PolicyToolExecutor
    from workbench_agent.jobs.worker import Execution
    with pytest.raises(ValueError): PolicyToolExecutor(platform.backend,object(),permissions=allow_permissions(platform,'read'))
    with pytest.raises((ValueError,TypeError)): PolicyToolExecutor(platform.backend,object(),execution=object())

@pytest.mark.asyncio
async def test_worker_authority_rejects_expert_or_provider_mutation(platform):
    from workbench_agent.jobs.worker import Worker,ExecutorRegistry
    from workbench_agent.tools.policy import PolicyToolExecutor
    from support.jobs import claim
    await platform.create_run(summary_token_reserve=8000)
    owned=await claim(platform)
    platform.backend._tool_permissions=allow_permissions(platform,'read')
    worker=Worker(platform.backend,'worker',ExecutorRegistry())
    worker._active_tool_claims[id(owned)]=owned  # Test harness; production run_once claims it.
    execution=worker._bind_tool_execution(owned)
    execution._expert_name='knowledge'
    with pytest.raises(ValueError): PolicyToolExecutor(platform.backend,object(),execution=execution)
    execution._expert_name='diagnosis'; execution.tool_permissions=allow_permissions(platform,'read')
    with pytest.raises(ValueError): PolicyToolExecutor(platform.backend,object(),execution=execution)

@pytest.mark.asyncio
async def test_historical_small_limit_returns_unknown_before_network(platform):
    from sqlalchemy import update
    from workbench_agent.persistence import schema
    from workbench_agent.application.idempotency import canonical,digest
    from workbench_agent.domain.tools import ToolBinding
    from workbench_agent.domain.profiles import ProfileDraft,VersionRef
    from workbench_agent.tools.registry import ToolRegistry
    from support.jobs import claim
    b=ToolBinding(alias='local.read',provider='mcp',tool_name='local.read',server='local',credential_scope='read',permission='read',input_schema={'type':'object'})
    ref=await ToolRegistry(platform.backend).publish(platform.auth,platform.agent_id,b,key='publish')
    raw=b.model_dump(mode='json');raw['max_output_bytes']=1;oldref=VersionRef(resource_id=ref.resource_id,version=ref.version,content_hash=digest(raw))
    async with platform.backend.create_uow().begin_write() as uow:
        await uow.execute(update(schema.tool_bindings).where(schema.tool_bindings.c.binding_id==ref.resource_id).values(schema=canonical(raw),hash=oldref.content_hash))
    ctx=await platform.contexts.resolve(platform.auth,platform.agent_id);ws=await platform.profiles.get_agent(platform.auth,platform.agent_id)
    draft=await platform.profiles.save_draft(platform.auth,platform.agent_id,ProfileDraft(default_model=ctx.default_model,tools=(oldref,)),ws['revision'],key='draft')
    await platform.profiles.publish(platform.auth,platform.agent_id,draft['revision'],key='profile')
    await platform.create_run(summary_token_reserve=8000);owned=await claim(platform)
    class Transport:
        calls=0
        async def call(self,*args,**kwargs): self.calls+=1
    transport=Transport();result=await executor_for(platform,owned,transport,'read').execute(owned.context,owned.guard,'old',oldref,{})
    assert result.status=='unknown' and result.error.code=='OUTPUT_LIMIT_UNSUPPORTED' and transport.calls==0

@pytest.mark.asyncio
async def test_foreign_worker_cannot_issue_tool_authority_for_valid_claim(platform):
    from workbench_agent.jobs.worker import Worker,ExecutorRegistry
    from workbench_agent.tools.policy import PolicyToolExecutor
    from support.jobs import claim
    await platform.create_run(summary_token_reserve=8000)
    owned=await claim(platform)
    with pytest.raises(TypeError):
        Worker(platform.backend,'attacker',ExecutorRegistry(),tool_permissions=allow_permissions(platform,'read'))
    fake=Worker(platform.backend,'attacker',ExecutorRegistry())
    with pytest.raises((ValueError,PermissionError)):
        forged=fake._bind_tool_execution(owned)
        PolicyToolExecutor(platform.backend,object(),execution=forged)

@pytest.mark.asyncio
async def test_real_worker_claim_lifecycle_issues_and_revokes_tool_authority(platform):
    from workbench_agent.domain.tools import ToolBinding
    from workbench_agent.domain.profiles import ProfileDraft
    from workbench_agent.tools.registry import ToolRegistry
    from workbench_agent.tools.policy import PolicyToolExecutor
    from workbench_agent.jobs.worker import Worker,ExecutorRegistry
    from support.jobs import ReleasingProjector
    b=ToolBinding(alias='local.read',provider='mcp',tool_name='local.read',server='local',credential_scope='read',permission='read',input_schema={'type':'object','properties':{'service':{'type':'string'}}})
    ref=await ToolRegistry(platform.backend).publish(platform.auth,platform.agent_id,b,key='publish')
    ctx=await platform.contexts.resolve(platform.auth,platform.agent_id);ws=await platform.profiles.get_agent(platform.auth,platform.agent_id)
    draft=await platform.profiles.save_draft(platform.auth,platform.agent_id,ProfileDraft(default_model=ctx.default_model,tools=(ref,)),ws['revision'],key='draft')
    await platform.profiles.publish(platform.auth,platform.agent_id,draft['revision'],key='profile')
    run=await platform.create_run(summary_token_reserve=8000)
    async with platform.backend.create_uow().begin_write() as uow:
        await platform.contexts.query_scope(uow,platform.auth,run.run_id,{'service':'svc'})
    platform.backend._tool_permissions=allow_permissions(platform,'read')
    retained=[]
    class Transport:
        async def call(self,*a,**k): return {'answer':'ok'}
    class Executor:
        async def execute(self,execution):
            executor=PolicyToolExecutor(platform.backend,Transport(),execution=execution)
            result=await executor.execute(execution.claim.context,execution.guard,'logical',ref,{'service':'svc'})
            assert result.status=='ok';retained.append(execution);return 'done'
    registry=ExecutorRegistry();registry.register('dod.v3',Executor(),ReleasingProjector(platform.backend))
    worker=Worker(platform.backend,'worker',registry)
    assert await worker.run_once()=='projected'
    with pytest.raises(ValueError): PolicyToolExecutor(platform.backend,Transport(),execution=retained[0])

@pytest.mark.asyncio
async def test_worker_keeps_other_claim_authority_when_one_slot_finishes(platform):
    import asyncio
    from workbench_agent.jobs.worker import Worker,ExecutorRegistry
    from workbench_agent.tools.policy import PolicyToolExecutor
    from support.jobs import ReleasingProjector
    first=await platform.create_run(key='first',summary_token_reserve=8000)
    await platform.create_run(key='second',summary_token_reserve=8000)
    platform.backend._tool_permissions=allow_permissions(platform,'read')
    waiting=asyncio.Event();release=asyncio.Event()
    class Executor:
        async def execute(self,execution):
            if execution.guard.run_id==first.run_id:
                waiting.set();await release.wait()
                PolicyToolExecutor(platform.backend,object(),execution=execution)
            return 'done'
    registry=ExecutorRegistry();registry.register('dod.v1',Executor(),ReleasingProjector(platform.backend))
    worker=Worker(platform.backend,'worker',registry)
    one=asyncio.create_task(worker.run_once());await asyncio.wait_for(waiting.wait(),5)
    assert await worker.run_once()=='projected'
    release.set();assert await one=='projected'

import pytest

@pytest.mark.asyncio
async def test_discovery_is_scoped_by_server_and_credential_and_closes_sessions():
    from workbench_agent.tools.mcp import MCPAdapter, ConnectionPolicy, TrustedConnection
    opened=[]
    class Session:
        async def __aenter__(self): opened.append(('open', self.server, self.credential)); return self
        async def __aexit__(self,*args): opened.append(('close', self.server, self.credential))
        async def list_tools(self): return [{'name':'knowledge'}]
    def factory(server, credential):
        session=Session(); session.server=server; session.credential=credential; return session
    policy=ConnectionPolicy([TrustedConnection('team','agent','server-a','scope-a'),TrustedConnection('team','agent','server-b','scope-b')])
    adapter=MCPAdapter(factory,policy)
    assert (await adapter.discover('server-a','scope-a',team_id='team',agent_id='agent')).names == ('knowledge',)
    assert (await adapter.discover('server-b','scope-b',team_id='team',agent_id='agent')).names == ('knowledge',)
    assert opened == [('open','server-a','scope-a'),('close','server-a','scope-a'),('open','server-b','scope-b'),('close','server-b','scope-b')]
    with pytest.raises(PermissionError): await adapter.discover('server-a','scope-b',team_id='team',agent_id='agent')
    assert len(opened)==4

@pytest.mark.parametrize('module,name', [('knowledge','KnowledgeRetrieveV2'),('monitoring','Common-Monitor-Query'),('logs','SRE-Log-Query'),('traces','SRE-Log-TraceQuery'),('dod','Common-DoD-GetInfo')])
def test_read_connector_names_are_registered_names(module,name):
    import importlib
    assert importlib.import_module('workbench_agent.connectors.'+module).TOOL_NAME==name

@pytest.mark.asyncio
async def test_deployment_policy_denies_unlisted_write_tool_before_session():
    from workbench_agent.tools.mcp import MCPAdapter, ConnectionPolicy, TrustedConnection
    opened=[]
    def factory(*args): opened.append(args); raise AssertionError('must not connect')
    policy=ConnectionPolicy([TrustedConnection('team','agent','server','reader',frozenset({'KnowledgeRetrieveV2'}))])
    adapter=MCPAdapter(factory,policy)
    with pytest.raises(PermissionError):
        await adapter.call('server','reader','Common-DoD-Call',{},team_id='team',agent_id='agent')
    assert opened==[]

@pytest.mark.asyncio
async def test_discovery_collects_all_pages_without_crossing_session():
    from types import SimpleNamespace
    from workbench_agent.tools.mcp import MCPAdapter,ConnectionPolicy,TrustedConnection
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        async def list_tools(self,cursor=None):
            return SimpleNamespace(tools=[{'name':'a' if cursor is None else 'b'}],nextCursor='next' if cursor is None else None)
    adapter=MCPAdapter(lambda *_:Session(),ConnectionPolicy([TrustedConnection('t','w','s','c')]))
    assert (await adapter.discover('s','c',team_id='t',agent_id='w')).names == ('a','b')

@pytest.mark.asyncio
async def test_official_streamable_http_client_against_local_mcp_server():
    import httpx
    from mcp.server.fastmcp import FastMCP
    from workbench_agent.tools.mcp import MCPAdapter,ConnectionPolicy,TrustedConnection,StreamableSessionFactory
    server=FastMCP('local-contract',stateless_http=True,json_response=True)
    @server.tool(name='knowledge_read',structured_output=True)
    def read(query:str)->dict[str,str]:
        return {'answer':query}
    app=server.streamable_http_app()
    async with app.router.lifespan_context(app):
        factory=StreamableSessionFactory({('local','read'):'https://localhost:8000/mcp'},lambda *_:{},transport=httpx.ASGITransport(app=app))
        adapter=MCPAdapter(factory,ConnectionPolicy([TrustedConnection('t','w','local','read',frozenset({'knowledge_read'}))]))
        catalog=await adapter.discover('local','read',team_id='t',agent_id='w')
        assert 'knowledge_read' in catalog.names
        result=await adapter.call('local','read','knowledge_read',{'query':'hello'},team_id='t',agent_id='w')
        assert not result.isError and result.structuredContent=={'answer':'hello'}

@pytest.mark.asyncio
async def test_configured_mcp_adapter_binds_endpoint_secret_and_scope():
    from workbench_agent.settings import MCPConnectionBinding
    from workbench_agent.tools.mcp import configured_adapter
    from uuid import uuid4
    team,agent=str(uuid4()),str(uuid4())
    binding=MCPConnectionBinding(server='knowledge',credential_scope='read',team_id=team,agent_id=agent,endpoint='https://example.invalid/mcp',secret_ref='env://MCP_TEST',read_tools=('KnowledgeRetrieveV2',))
    class Secrets:
        def __init__(self): self.refs=[]
        def resolve(self,ref): self.refs.append(ref); return 'fictional-secret'
    secrets=Secrets()
    adapter=configured_adapter([binding],secrets)
    assert adapter is not None
    adapter.policy.require(team,agent,'knowledge','read','KnowledgeRetrieveV2')
    with pytest.raises(PermissionError): adapter.policy.require(team,agent,'knowledge','read','Common-DoD-Call')
    assert secrets.refs==[]
    assert adapter.session_factory.headers('knowledge','read')=={'Authorization':'Bearer fictional-secret'}
    assert secrets.refs==['env://MCP_TEST']

@pytest.mark.asyncio
async def test_changed_discovered_schema_prevents_actual_call():
    from types import SimpleNamespace
    from workbench_agent.tools.mcp import MCPAdapter,ConnectionPolicy,TrustedConnection,SchemaChanged
    class Session:
        called=False
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        async def list_tools(self): return SimpleNamespace(tools=[{'name':'read','inputSchema':{'type':'integer'}}],nextCursor=None)
        async def call_tool(self,*args): self.called=True
    session=Session()
    adapter=MCPAdapter(lambda *_:session,ConnectionPolicy([TrustedConnection('t','w','s','c',frozenset({'read'}))]))
    with pytest.raises(SchemaChanged):
        await adapter.call('s','c','read',{},team_id='t',agent_id='w',expected_schema={'type':'object'})
    assert not session.called

@pytest.mark.asyncio
@pytest.mark.parametrize('name',['SRE-Log-Query','SRE-Log-TraceQuery'])
async def test_log_session_key_never_reaches_server_even_if_configured_read(name):
    from workbench_agent.tools.mcp import MCPAdapter,ConnectionPolicy,TrustedConnection
    opened=[]
    def factory(*args): opened.append(args); raise AssertionError('must not connect')
    adapter=MCPAdapter(factory,ConnectionPolicy([TrustedConnection('t','w','s','c',frozenset({name}))]))
    with pytest.raises(PermissionError):
        await adapter.call('s','c',name,{'session_key':'untrusted'},team_id='t',agent_id='w')
    assert opened==[]

@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['KnowledgeRetrieveV2','Common-Monitor-Query','SRE-Log-Query','SRE-Log-TraceQuery','Common-DoD-GetInfo'])
async def test_business_calls_without_trusted_scope_mapping_are_denied(name):
    from workbench_agent.tools.mcp import MCPAdapter,ConnectionPolicy,TrustedConnection
    called=[]
    def factory(*args): called.append(args); raise AssertionError('must not connect')
    adapter=MCPAdapter(factory,ConnectionPolicy([TrustedConnection('t','w','s','c',frozenset({name}))]))
    with pytest.raises(PermissionError):
        await adapter.call('s','c',name,{'query':'untrusted'},team_id='t',agent_id='w')
    assert not called

def test_deployment_rejects_unreviewed_tool_even_if_called_read_only():
    from uuid import uuid4
    from workbench_agent.settings import MCPConnectionBinding
    from workbench_agent.tools.mcp import configured_adapter
    b=MCPConnectionBinding(server='s',credential_scope='c',team_id=str(uuid4()),agent_id=str(uuid4()),endpoint='https://example.invalid/mcp',secret_ref='env://MCP_TEST',read_tools=('unreviewed_write',))
    with pytest.raises(ValueError,match='unreviewed'):
        configured_adapter([b],object())

def test_log_arguments_are_built_from_exact_deployment_grant():
    from workbench_agent.tools.mcp import build_business_arguments
    scope={'service':'svc.test','environment':'test','region':'sg','start_at':'2026-01-01 00:00:00','end_at':'2026-01-01 00:10:00'}
    grant={'scope':scope,'team':'team-a'}
    args=build_business_arguments('SRE-Log-Query',{'query':'ERROR'},scope,grant)
    assert args=={'query':'ERROR','team':'team-a','env':'test','region':'sg','service_cmdb_name':'svc.test','begin_time':scope['start_at'],'end_time':scope['end_at']}
    with pytest.raises(PermissionError): build_business_arguments('SRE-Log-Query',{'query':'ERROR','env':'live'},scope,grant)
    with pytest.raises(PermissionError): build_business_arguments('SRE-Log-Query',{'query':'ERROR'},dict(scope,region='id'),grant)

def test_duty_query_uses_deployment_team_and_rejects_session_override():
    from workbench_agent.tools.mcp import build_business_arguments
    from datetime import datetime,timedelta
    from zoneinfo import ZoneInfo
    now=datetime.now(ZoneInfo('Asia/Shanghai'))
    scope={'service':'svc.test','environment':'test','region':'sg','start_at':(now-timedelta(minutes=1)).strftime('%Y-%m-%d %H:%M:%S'),'end_at':(now+timedelta(minutes=1)).strftime('%Y-%m-%d %H:%M:%S')}
    grant={'scope':scope,'team':'team-a'}
    assert build_business_arguments('Common-DoD-GetInfo',{},scope,grant)=={'team':'team-a'}
    with pytest.raises(PermissionError):
        build_business_arguments('Common-DoD-GetInfo',{'smart_thread_id':'other'},scope,grant)

def test_duty_rejects_historical_window_even_if_granted():
    from workbench_agent.tools.mcp import build_business_arguments
    scope={'service':'svc','environment':'test','region':'sg','start_at':'2020-01-01 00:00:00','end_at':'2020-01-01 00:10:00'}
    with pytest.raises(PermissionError): build_business_arguments('Common-DoD-GetInfo',{},scope,{'scope':scope,'team':'t'})

def test_scope_grant_allows_only_bounded_time_ranges():
    from workbench_agent.tools.mcp import build_business_arguments
    scope={'service':'svc.test','environment':'test','region':'sg','start_at':'2026-01-01 02:00:00','end_at':'2026-01-01 02:10:00'}
    grant={'team':'team-a','allowed_scope':{'service':'svc.test','environment':'test','region':'sg'},'earliest':'2026-01-01 00:00:00','latest':'2026-01-02 00:00:00','max_span_seconds':3600}
    assert build_business_arguments('SRE-Log-Query',{'query':'ERROR'},scope,grant)['team']=='team-a'
    with pytest.raises(PermissionError):
        build_business_arguments('SRE-Log-Query',{'query':'ERROR'},dict(scope,end_at='2026-01-01 04:00:00'),grant)
    with pytest.raises(PermissionError):
        build_business_arguments('SRE-Log-Query',{'query':'ERROR'},dict(scope,start_at='2025-12-31 23:59:00'),grant)

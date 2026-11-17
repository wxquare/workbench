"""Opt-in read-only business probes; endpoints and cases are never guessed.

WORKBENCH_MCP_LIVE_CASES_FILE points to a locally provided JSON file. Each named
entry contains connection (deployment MCPConnectionBinding), binding
(ToolBinding), scope and args. Supply only secret references, not values.
"""
import json
import os
from pathlib import Path
import pytest
from workbench_agent.domain.profiles import ProfileDraft
from workbench_agent.domain.tools import ToolBinding
from workbench_agent.settings import MCPConnectionBinding
from workbench_agent.tools.mcp import configured_adapter
from workbench_agent.tools.policy import PolicyToolExecutor
from workbench_agent.tools.permissions import DeploymentToolPermissions
from workbench_agent.tools.registry import ToolRegistry
from workbench_agent.connectors.secrets import SecretProvider
from support.jobs import claim

pytestmark=pytest.mark.live

@pytest.mark.asyncio
@pytest.mark.parametrize(('case','tool_name'),[
    ('knowledge','KnowledgeRetrieveV2'),('monitoring','Common-Monitor-Query'),
    ('logs','SRE-Log-Query'),('traces','SRE-Log-TraceQuery'),('dod','Common-DoD-GetInfo')])
async def test_authorized_read_connector(platform,case,tool_name):
    path=os.environ.get('WORKBENCH_MCP_LIVE_CASES_FILE')
    if not path: pytest.skip('authorized nonproduction MCP cases not configured')
    cases=json.loads(Path(path).read_text())
    if case not in cases: pytest.skip('authorized case not configured')
    entry=cases[case]
    connection=MCPConnectionBinding.model_validate(entry['connection'])
    binding=ToolBinding.model_validate(entry['binding'])
    assert connection.team_id==platform.auth.team_id and connection.agent_id==platform.agent_id
    assert binding.tool_name==tool_name and binding.read_only
    assert tool_name in connection.read_tools
    adapter=configured_adapter((connection,),SecretProvider())
    catalog=await adapter.discover(binding.server,binding.credential_scope,team_id=platform.auth.team_id,agent_id=platform.agent_id)
    assert tool_name in catalog.names
    ref=await ToolRegistry(platform.backend).publish(platform.auth,platform.agent_id,binding,key='live-tool')
    current=await platform.contexts.resolve(platform.auth,platform.agent_id)
    agent=await platform.profiles.get_agent(platform.auth,platform.agent_id)
    draft=await platform.profiles.save_draft(platform.auth,platform.agent_id,ProfileDraft(default_model=current.default_model,tools=(ref,)),agent['revision'],key='live-draft')
    await platform.profiles.publish(platform.auth,platform.agent_id,draft['revision'],key='live-profile')
    run=await platform.create_run(summary_token_reserve=8000)
    async with platform.backend.create_uow().begin_write() as uow:
        await platform.contexts.query_scope(uow,platform.auth,run.run_id,entry['scope'])
    owned=await claim(platform)
    rules={'users':{platform.auth.subject:[binding.permission]},'teams':{platform.auth.team_id:[binding.permission]},
        'agents':{platform.agent_id:[binding.permission]},'skills':{'__none__':[binding.permission]},'experts':{'diagnosis':[binding.permission]}}
    from workbench_agent.jobs.worker import Worker,ExecutorRegistry
    # This live harness constructs its authority explicitly for a disposable test DB.
    platform.backend._tool_permissions=DeploymentToolPermissions(lambda:rules)
    worker=Worker(platform.backend,owned.guard.worker_id,ExecutorRegistry())
    worker._active_tool_claims[id(owned)]=owned
    executor=PolicyToolExecutor(platform.backend,adapter,execution=worker._bind_tool_execution(owned))
    result=await executor.execute(owned.context,owned.guard,'live-read',ref,entry['args'])
    assert result.status in ('ok','empty')

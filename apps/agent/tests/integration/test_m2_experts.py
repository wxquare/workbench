from types import SimpleNamespace

import pytest

from workbench_agent.domain.tools import ToolBinding
from workbench_agent.runtime.graphs.tool_conversation import _conversation_tool_allowed


@pytest.mark.parametrize(
    'name',
    [
        'SRE-Log-Query',
        'SRE-Log-TraceQuery',
        'Common-DoD-GetInfo',
        'metrics_query',
        'logs_search',
        'trace_get',
    ],
)
def test_unmapped_business_tools_are_not_admitted_to_model_conversation(name):
    binding = ToolBinding(
        alias='fixture.read',
        provider='mcp',
        tool_name=name,
        server='local',
        credential_scope='read',
        permission='read',
        input_schema={'type': 'object'},
    )
    backend = SimpleNamespace(_mcp_connections=())
    context = SimpleNamespace(team_id='team', agent_id='agent')
    assert not _conversation_tool_allowed(backend, context, binding)

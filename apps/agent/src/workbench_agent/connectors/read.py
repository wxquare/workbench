"""Read-only connector façade; all execution passes through tool policy."""
from dataclasses import dataclass
from workbench_agent.domain.context import RunContext
from workbench_agent.domain.guards import ExecutionGuard
from workbench_agent.domain.profiles import VersionRef
from workbench_agent.domain.tools import ToolResult

@dataclass(frozen=True)
class ConnectorContext:
    run: RunContext
    guard: ExecutionGuard
    invocation_id: str
    binding_ref: VersionRef

ConnectorResult=ToolResult

class ReadConnector:
    tool_name: str
    def __init__(self,executor): self.executor=executor
    async def query(self,request:dict,context:ConnectorContext)->ConnectorResult:
        return await self.executor.execute(context.run,context.guard,context.invocation_id,context.binding_ref,request,expected_tool=self.tool_name)

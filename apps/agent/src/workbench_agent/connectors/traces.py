from workbench_agent.connectors.read import ReadConnector

TOOL_NAME = 'SRE-Log-TraceQuery'


class TraceConnector(ReadConnector):
    tool_name = TOOL_NAME

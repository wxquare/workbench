from workbench_agent.connectors.read import ReadConnector

TOOL_NAME = 'SRE-Log-Query'


class LogsConnector(ReadConnector):
    tool_name = TOOL_NAME

import json
import logging


class LoggingAudit:
    def emit(self, event):
        logging.getLogger("workbench_mcp.audit").info(json.dumps(event, ensure_ascii=False))

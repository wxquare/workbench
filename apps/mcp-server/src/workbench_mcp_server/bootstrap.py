import httpx

from workbench_mcp_server.application.use_cases import QueryExecutor
from workbench_mcp_server.infrastructure.audit import LoggingAudit
from workbench_mcp_server.infrastructure.auth.bearer import BearerAuthenticator
from workbench_mcp_server.infrastructure.clock import SystemClock
from workbench_mcp_server.infrastructure.config.secrets import EnvSecrets
from workbench_mcp_server.infrastructure.connectors.knowledge_v2 import KnowledgeV2Gateway
from workbench_mcp_server.infrastructure.connectors.normalized_http import NormalizedHttpGateway
from workbench_mcp_server.infrastructure.cursor import SignedCursor
from workbench_mcp_server.interfaces.mcp.mapping import OutputMapper


class Runtime:
    def __init__(self, settings):
        self.settings = settings
        secrets = EnvSecrets()
        self.authenticator = BearerAuthenticator(settings, secrets)
        self.stdio_principal = (
            settings.principal(settings.server.stdio_principal)
            if settings.server.stdio_principal
            else None
        )
        tokens = {s.id: secrets.resolve(s.auth_secret_ref) for s in settings.sources}
        redactions = list(tokens.values()) + [
            secrets.resolve(p.token_secret_ref) for p in settings.principals
        ]
        clock = SystemClock()
        cursor = None
        if settings.cursor_secret_ref:
            key = secrets.resolve(settings.cursor_secret_ref)
            redactions.append(key)
            cursor = SignedCursor(key, clock)
        self._clients, gateways = [], {}
        for source in settings.sources:
            client = httpx.AsyncClient(trust_env=False, follow_redirects=False)
            self._clients.append(client)
            cls = (
                KnowledgeV2Gateway
                if source.adapter == "knowledge-v2"
                else NormalizedHttpGateway
            )
            gateways[source.id] = cls(
                source, client, tokens[source.id], settings.limits.upstream_bytes
            )
        self.audit = LoggingAudit()
        self.executor = QueryExecutor(
            settings.runtime_sources(), gateways, settings.limits, clock, self.audit, cursor
        )
        self.output = OutputMapper(
            settings.limits.response_bytes,
            redactions,
            [h for s in settings.sources for h in s.allowed_source_hosts],
            settings.audit.redact_fields,
        )

    async def close(self):
        for client in self._clients:
            await client.aclose()

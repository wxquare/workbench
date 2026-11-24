"""Read-only local deployment checks without resolving or returning secrets."""

from __future__ import annotations

from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit

from workbench_agent.evaluation.contracts import validate_experiment_config


def _check(ok: bool, code: str, message: str) -> dict:
    return {'ok': bool(ok), 'code': code, 'message': message}


def _numeric_loopback(value: str | None) -> bool:
    if not value:
        return False
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme == 'http'
            and bool(parsed.hostname)
            and ip_address(parsed.hostname).is_loopback
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
        )
    except (ValueError, TypeError):
        return False


def preflight(config: dict, *, environ=None, settings=None) -> dict:
    """Validate deploy configuration; an absent token remains a reported blocker."""
    import os

    environ = os.environ if environ is None else environ
    checks: list[dict] = []
    try:
        validate_experiment_config(config)
        checks.append(_check(True, 'SESSION_CONFIG', 'session configuration is valid'))
    except (ValueError, TypeError) as error:
        checks.append(_check(False, 'SESSION_CONFIG', str(error)))
    api = urlsplit(str(config.get('api_base_url', '')))
    try:
        api_port = api.port
    except ValueError:
        api_port = None
    checks.append(
        _check(
            api.hostname in {'127.0.0.1', '::1'} and api_port == 8000,
            'AGENT_ENDPOINT',
            'Agent API must use local port 8000',
        )
    )
    checks.append(
        _check(
            bool(environ.get(config.get('token_env', ''))),
            'API_TOKEN',
            'the configured API token environment variable is unset',
        )
    )
    if settings is not None:
        mcp = settings.service.mcp_connections
        checks.append(
            _check(
                bool(mcp),
                'MCP_CONNECTION',
                'an Agent MCP connection is required',
            )
        )
        if mcp:
            endpoint = mcp[0].endpoint
            parsed = urlsplit(endpoint)
            try:
                mcp_port = parsed.port
            except ValueError:
                mcp_port = None
            checks.append(
                _check(
                    _numeric_loopback(endpoint) and mcp_port == 8001 and mcp[0].allow_loopback_http,
                    'MCP_PORT',
                    'MCP must use explicitly enabled numeric loopback port 8001',
                )
            )
            checks.append(
                _check(
                    'knowledge_search' in mcp[0].read_tools,
                    'KNOWLEDGE_TOOL',
                    'knowledge_search must be an allowed read tool',
                )
            )
        knowledge = settings.knowledge
        checks.append(
            _check(
                knowledge.enabled and bool(knowledge.grants),
                'KNOWLEDGE_ENABLED',
                'knowledge access requires enabled grants',
            )
        )
        checks.append(
            _check(
                knowledge.public_base_url == config.get('api_base_url'),
                'KNOWLEDGE_URL',
                'knowledge public URL must match the local Agent URL',
            )
        )
        supported = settings.worker.supported_graph_ids
        checks.append(
            _check(
                config.get('expected_graph_id') in supported,
                'WORKER_GRAPH',
                'Worker must enable the expected knowledge graph',
            )
        )
        if knowledge.grants:
            grant = knowledge.grants[0]
            agent_binding = mcp[0] if mcp else None
            checks.append(
                _check(
                    agent_binding is not None and str(grant.team_id) == agent_binding.team_id,
                    'TEAM_SCOPE',
                    'knowledge grant and MCP binding teams must match',
                )
            )
            checks.append(
                _check(
                    bool(environ.get(grant.token_secret_ref.removeprefix('env://'))),
                    'UPSTREAM_TOKEN',
                    'the internal knowledge token is unset',
                )
            )
    for field in ('agent_config_path', 'mcp_config_path'):
        raw = config.get(field)
        path = Path(raw) if isinstance(raw, str) and raw else None
        checks.append(
            _check(
                path is not None and path.is_file(),
                'CONFIG_FILE',
                f'{field} does not exist',
            )
        )
    result = {
        'ready': all(item['ok'] for item in checks),
        'checks': checks,
    }
    # Fail closed if a provider accidentally embeds a secret in a check.
    secret_names = {config.get('token_env')}
    if settings is not None:
        secret_names.update(
            grant.token_secret_ref.removeprefix('env://') for grant in settings.knowledge.grants
        )
    secret_values = [item for key, item in environ.items() if key in secret_names and item]
    text = str(result)
    if any(secret in text for secret in secret_values):
        return {
            'ready': False,
            'checks': [_check(False, 'SECRET_REDACTION', 'secret redaction failed')],
        }
    return result

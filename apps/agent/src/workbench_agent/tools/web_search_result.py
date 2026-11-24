"""Strict validation and safe projection of public web search snippets."""
from __future__ import annotations

from datetime import datetime
from ipaddress import ip_address
from urllib.parse import urlsplit

STATUSES = {'ok', 'empty', 'unavailable', 'denied'}
FIELDS = {
    'schema_version',
    'status',
    'results',
    'retrieved_at',
    'truncated',
    'domain_policy_id',
    'content_kind',
}
RESULT_FIELDS = {'title', 'url', 'snippet', 'published_at'}


def _text(value, maximum):
    return (
        isinstance(value, str)
        and value == value.strip()
        and bool(value)
        and len(value) <= maximum
        and not any(ord(character) < 32 and character not in '\n\t' for character in value)
    )


def _timestamp(value, *, nullable=False):
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise ValueError('WEB_SEARCH_INVALID')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        raise ValueError('WEB_SEARCH_INVALID') from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError('WEB_SEARCH_INVALID')
    return value


def _host(value):
    parsed = urlsplit(value)
    if (
        parsed.scheme != 'https'
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError('WEB_SEARCH_URL_DENIED')
    host = parsed.hostname.rstrip('.').encode('idna').decode('ascii').casefold()
    try:
        address = ip_address(host)
    except ValueError:
        address = None
    if address is not None and (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    ):
        raise ValueError('WEB_SEARCH_URL_DENIED')
    return host


def web_search_grant(policy, *, team_id, agent_id, binding):
    """Find exactly one current deployment grant for the published tool."""
    matches = []
    for rule in getattr(policy, '_bindings', lambda: ())():
        if (
            rule.team_id == team_id
            and rule.agent_id == agent_id
            and rule.server == binding.server
            and rule.credential_scope == binding.credential_scope
            and 'web_search' in rule.read_tools
        ):
            for grant in rule.scope_grants:
                if (
                    isinstance(grant, dict)
                    and isinstance(grant.get('domain_policy_id'), str)
                    and isinstance(grant.get('allowed_result_hosts'), list)
                ):
                    matches.append(grant)
    if len(matches) != 1:
        raise PermissionError('WEB_SEARCH_POLICY_UNAVAILABLE')
    return matches[0]


def validate_web_search_result(
    value: dict,
    *,
    expected_policy_id: str,
    allowed_hosts: tuple[str, ...],
) -> dict:
    if not isinstance(value, dict) or set(value) != FIELDS:
        raise ValueError('WEB_SEARCH_INVALID')
    if value.get('schema_version') != 'web-search-result-v1':
        raise ValueError('WEB_SEARCH_INVALID')
    if value.get('status') not in STATUSES:
        raise ValueError('WEB_SEARCH_INVALID')
    if value.get('domain_policy_id') != expected_policy_id:
        raise ValueError('WEB_SEARCH_POLICY_MISMATCH')
    if value.get('content_kind') != 'search_snippet':
        raise ValueError('WEB_SEARCH_INVALID')
    if type(value.get('truncated')) is not bool:
        raise ValueError('WEB_SEARCH_INVALID')
    retrieved_at = value.get('retrieved_at')
    _timestamp(retrieved_at)
    results = value.get('results')
    if not isinstance(results, list) or len(results) > 5:
        raise ValueError('WEB_SEARCH_INVALID')
    if value['status'] != 'ok' and results:
        raise ValueError('WEB_SEARCH_INVALID')
    normalized = {
        **value,
        'retrieved_at': retrieved_at,
        'results': [],
    }
    allowed = {
        host.rstrip('.').encode('idna').decode('ascii').casefold()
        for host in allowed_hosts
    }
    for result in results:
        if not isinstance(result, dict) or set(result) != RESULT_FIELDS:
            raise ValueError('WEB_SEARCH_INVALID')
        title, url, snippet = (
            result.get('title'),
            result.get('url'),
            result.get('snippet'),
        )
        if (
            not _text(title, 512)
            or not _text(snippet, 4000)
            or not _text(url, 2048)
        ):
            raise ValueError('WEB_SEARCH_INVALID')
        host = _host(url)
        if host not in allowed:
            raise ValueError('WEB_SEARCH_URL_DENIED')
        published = _timestamp(result.get('published_at'), nullable=True)
        normalized['results'].append({
            'title': title,
            'url': url,
            'snippet': snippet,
            'published_at': published,
        })
    return normalized


def validate_web_search_response(
    response,
    *,
    policy,
    team_id,
    agent_id,
    binding,
):
    grant = web_search_grant(
        policy, team_id=team_id, agent_id=agent_id, binding=binding
    )
    try:
        value = response.structuredContent
    except AttributeError:
        value = response
    if getattr(response, 'isError', False):
        return {
            'status': 'unavailable',
            'error': 'TOOL_PROVIDER_ERROR',
            'grant': grant,
        }
    try:
        normalized = validate_web_search_result(
            value,
            expected_policy_id=grant['domain_policy_id'],
            allowed_hosts=tuple(grant['allowed_result_hosts']),
        )
    except ValueError as error:
        status = (
            'denied'
            if str(error) == 'WEB_SEARCH_URL_DENIED'
            else 'unknown'
        )
        return {'status': status, 'error': str(error), 'grant': grant}
    return {**normalized, 'grant': grant}

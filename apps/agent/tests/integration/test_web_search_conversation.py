from pathlib import Path
from types import SimpleNamespace

import pytest

from workbench_agent.domain.tools import ToolBinding
from workbench_agent.tools.mcp import ConnectionPolicy, TrustedConnection
from workbench_agent.tools.web_search_result import validate_web_search_response
from workbench_agent.tools.policy import PolicyToolExecutor


def binding():
    return ToolBinding(
        alias='web.search',
        provider='mcp',
        tool_name='web_search',
        server='web',
        credential_scope='read',
        permission='web.read',
        input_schema={
            'type': 'object',
            'properties': {'query': {'type': 'string'}},
            'required': ['query'],
        },
    )


def policy():
    return ConnectionPolicy(
        [
            TrustedConnection(
                'team',
                'agent',
                'web',
                'read',
                frozenset({'web_search'}),
                (
                    {
                        'result_limit': 5,
                        'domain_policy_id': 'public-docs-v1',
                        'allowed_result_hosts': ['docs.example.com'],
                    },
                ),
            )
        ]
    )


def response(status='ok', results=None):
    return SimpleNamespace(
        isError=False,
        structuredContent={
            'schema_version': 'web-search-result-v1',
            'status': status,
            'results': results or [],
            'retrieved_at': '2026-11-24T00:00:00Z',
            'truncated': False,
            'domain_policy_id': 'public-docs-v1',
            'content_kind': 'search_snippet',
        },
    )


def test_empty_and_denied_statuses_are_preserved():
    empty = validate_web_search_response(
        response('empty'),
        policy=policy(),
        team_id='team',
        agent_id='agent',
        binding=binding(),
    )
    denied = validate_web_search_response(
        response('denied'),
        policy=policy(),
        team_id='team',
        agent_id='agent',
        binding=binding(),
    )
    assert empty['status'] == 'empty'
    assert denied['status'] == 'denied'


def test_missing_web_grant_is_rejected():
    with pytest.raises(PermissionError):
        validate_web_search_response(
            response(),
            policy=ConnectionPolicy([]),
            team_id='team',
            agent_id='agent',
            binding=binding(),
        )


def test_bad_url_cannot_be_reported_as_success():
    result = response(
        results=[
            {
                'title': 'Bad',
                'url': 'https://docs.example.com.attacker.test/a',
                'snippet': 'data',
                'published_at': None,
            }
        ]
    )
    value = validate_web_search_response(
        result,
        policy=policy(),
        team_id='team',
        agent_id='agent',
        binding=binding(),
    )
    assert value['status'] == 'denied'


def test_policy_executor_routes_web_envelope_before_generic_mapping():
    source = Path(__file__).parents[2] / 'src/workbench_agent/tools/policy.py'
    text = source.read_text()
    assert "if binding.tool_name == 'web_search':" in text
    assert 'validate_web_search_response(' in text

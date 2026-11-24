import json
from pathlib import Path

from workbench_agent.tools.mcp import MCPAdapter, ConnectionPolicy, TrustedConnection
from workbench_agent.tools.scope_mapping import (
    ArgumentMapping,
    FieldMapping,
    mapping_fingerprint,
    mapping_hash,
)


ROOT = Path(__file__).parents[4]
MODEL_SCHEMA = {
    'type': 'object',
    'additionalProperties': False,
    'properties': {
        'query': {'type': 'string', 'minLength': 1, 'maxLength': 1000},
    },
    'required': ['query'],
}
FULL_SCHEMA = {
    'type': 'object',
    'additionalProperties': False,
    'properties': {
        'query': {'type': 'string', 'minLength': 1, 'maxLength': 1000},
        'limit': {'type': 'integer', 'minimum': 1, 'maximum': 5},
        'domain_policy_id': {'type': 'string'},
        'context': {
            'type': 'object',
            'additionalProperties': False,
            'properties': {
                'run_id': {'type': 'string'},
                'invocation_id': {'type': 'string'},
                'query_scope_ref': {'type': 'string'},
            },
            'required': ['run_id', 'invocation_id', 'query_scope_ref'],
        },
    },
    'required': ['query', 'limit', 'domain_policy_id', 'context'],
}


def mapping():
    return ArgumentMapping(
        model_input_schema=MODEL_SCHEMA,
        fields=(
            FieldMapping(origin='model', source='query', target=('query',)),
            FieldMapping(origin='grant', source='result_limit', target=('limit',)),
            FieldMapping(
                origin='grant',
                source='domain_policy_id',
                target=('domain_policy_id',),
            ),
            FieldMapping(origin='context', source='run_id', target=('context', 'run_id')),
            FieldMapping(
                origin='context',
                source='invocation_id',
                target=('context', 'invocation_id'),
            ),
            FieldMapping(
                origin='context',
                source='query_scope_ref',
                target=('context', 'query_scope_ref'),
            ),
        ),
    )


def policy():
    return {
        'tool_name': 'web_search',
        'mapping_hash': mapping_hash(mapping()),
        'model_input_fields': ['query'],
        'query_scope_fields': [],
        'grant_fields': ['result_limit', 'domain_policy_id'],
        'context_fields': ['run_id', 'invocation_id', 'query_scope_ref'],
        'protected_targets': [
            ['limit'],
            ['domain_policy_id'],
            ['context', 'run_id'],
            ['context', 'invocation_id'],
            ['context', 'query_scope_ref'],
        ],
    }


def adapter():
    return MCPAdapter(
        lambda *_: None,
        ConnectionPolicy([
            TrustedConnection(
                '11111111-1111-1111-1111-111111111111',
                '22222222-2222-2222-2222-222222222222',
                'web',
                'read',
                frozenset({'web_search'}),
                ({
                    'result_limit': 5,
                    'domain_policy_id': 'public-docs-v1',
                    'allowed_result_hosts': ['docs.example.com'],
                },),
                (policy(),),
                frozenset({'web_search'}),
            )
        ]),
    )


def prepare(mapping_value=None, args=None):
    return adapter().prepare(
        'web',
        'read',
        'web_search',
        args or {'query': 'SQLite WAL documentation'},
        team_id='11111111-1111-1111-1111-111111111111',
        agent_id='22222222-2222-2222-2222-222222222222',
        query_scope={'unused': 'ignored'},
        correlation={
            'run_id': 'run',
            'invocation_id': 'invocation',
            'query_scope_ref': 'scope',
        },
        argument_mapping=mapping_value or mapping(),
        full_schema=FULL_SCHEMA,
    )


def test_web_search_maps_only_query_and_injects_trusted_values():
    prepared = prepare()
    assert prepared == {
        'query': 'SQLite WAL documentation',
        'limit': 5,
        'domain_policy_id': 'public-docs-v1',
        'context': {
            'run_id': 'run',
            'invocation_id': 'invocation',
            'query_scope_ref': 'scope',
        },
    }
    assert 'service' not in prepared


def test_web_search_model_cannot_override_protected_fields():
    try:
        prepare(args={'query': 'x', 'limit': 50})
    except PermissionError:
        pass
    else:
        raise AssertionError('protected field accepted')


def test_declaration_files_pin_the_same_contract():
    binding = json.loads(
        (ROOT / 'config/examples/web-search-binding.json').read_text()
    )
    deployment = (
        ROOT / 'config/examples/web-search-deployment.yaml'
    ).read_text()
    contract = json.loads(
        (ROOT / 'evaluation/tool-conversation/web-search-contract.json').read_text()
    )
    assert binding['argument_mapping'] == mapping().model_dump(mode='json')
    assert binding['input_schema'] == FULL_SCHEMA
    assert contract['input_schema'] == FULL_SCHEMA
    assert contract['mapping_fingerprint'] == mapping_fingerprint(
        mapping(), FULL_SCHEMA
    )
    assert 'web_search' in deployment
    assert 'web.read' in deployment

import json

import pytest

from test_scoped_mcp_mapping import CONTEXT, GRANT, SCOPE
from workbench_agent.tools.mcp import ConnectionPolicy, MCPAdapter, TrustedConnection
from workbench_agent.tools.scope_mapping import (
    ArgumentMapping,
    FieldMapping,
    mapping_hash,
)


def knowledge_mapping():
    return ArgumentMapping(
        fields=(
            FieldMapping(origin='model', source='query', target=('query',)),
            FieldMapping(origin='model', source='scene', target=('scene',)),
            FieldMapping(
                origin='query_scope', source='environment', target=('scope', 'environment')
            ),
            FieldMapping(origin='query_scope', source='region', target=('scope', 'region')),
            FieldMapping(origin='query_scope', source='service', target=('scope', 'service')),
            FieldMapping(origin='grant', source='team', target=('scope', 'team')),
            FieldMapping(
                origin='grant', source='sources.knowledge_search', target=('scope', 'source_id')
            ),
            FieldMapping(origin='context', source='run_id', target=('context', 'run_id')),
            FieldMapping(
                origin='context', source='invocation_id', target=('context', 'invocation_id')
            ),
            FieldMapping(
                origin='context', source='query_scope_ref', target=('context', 'query_scope_ref')
            ),
        ),
        model_input_schema={
            'type': 'object',
            'additionalProperties': False,
            'properties': {
                'query': {'type': 'string'},
                'scene': {'type': 'string'},
            },
            'required': ['query', 'scene'],
        },
    )


def policy():
    value = knowledge_mapping()
    return {
        'tool_name': 'knowledge_search',
        'mapping_hash': mapping_hash(value),
        'model_input_fields': ['query', 'scene'],
        'query_scope_fields': ['environment', 'region', 'service'],
        'grant_fields': ['team', 'sources.knowledge_search'],
        'context_fields': ['run_id', 'invocation_id', 'query_scope_ref'],
        'protected_targets': [
            ['scope', 'team'],
            ['scope', 'environment'],
            ['scope', 'region'],
            ['scope', 'service'],
            ['scope', 'source_id'],
            ['context', 'run_id'],
            ['context', 'invocation_id'],
            ['context', 'query_scope_ref'],
        ],
    }


class Adapter:
    def __init__(self):
        self.mapping_policies = {'knowledge_search': policy()}

    def prepare(self, connection_ref, credential_scope, tool_name, args, **kwargs):
        from workbench_agent.tools.scope_mapping import (
            bind_arguments,
            validate_mapping,
        )
        from workbench_agent.domain.tools import ToolBinding

        full_schema = {
            'type': 'object',
            'additionalProperties': False,
            'properties': {
                'query': {'type': 'string'},
                'scene': {'type': 'string'},
                'scope': {
                    'type': 'object',
                    'additionalProperties': False,
                    'properties': {
                        'team': {'type': 'string'},
                        'environment': {'type': 'string'},
                        'region': {'type': 'string'},
                        'service': {'type': 'string'},
                        'source_id': {'type': 'string'},
                    },
                    'required': ['team', 'environment', 'region', 'service', 'source_id'],
                },
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
            'required': ['query', 'scene', 'scope', 'context'],
        }
        value = knowledge_mapping()
        validate_mapping(value, full_schema, policy())
        return bind_arguments(
            value,
            args,
            query_scope=kwargs['query_scope'],
            grant=GRANT,
            context=kwargs['correlation'],
        )


def adapter():
    return MCPAdapter(
        None,
        ConnectionPolicy(
            [
                TrustedConnection(
                    'team',
                    'agent',
                    'local',
                    'read',
                    frozenset({'test_read_counter'}),
                    (GRANT,),
                    (policy(),),
                )
            ]
        ),
    )


def test_declarative_knowledge_mapping_equals_legacy_contract():
    adapter = Adapter()
    args = {'query': 'help', 'scene': 'qa'}
    mapped = adapter.prepare(
        'local',
        'read',
        'knowledge_search',
        args,
        team_id='team',
        agent_id='agent',
        query_scope=SCOPE,
        correlation=CONTEXT,
    )
    assert mapped == {
        'query': 'help',
        'scene': 'qa',
        'scope': {
            'team': 'ExampleTeam',
            'environment': 'test',
            'region': 'id',
            'service': 'checkout',
            'source_id': 'kb',
        },
        'context': CONTEXT,
    }


def test_declarative_mapping_rejects_model_scope_injection():
    adapter = Adapter()
    with pytest.raises(PermissionError):
        adapter.prepare(
            'local',
            'read',
            'knowledge_search',
            {
                'query': 'help',
                'scene': 'qa',
                'scope': {'team': 'attacker'},
            },
            team_id='team',
            agent_id='agent',
            query_scope=SCOPE,
            correlation=CONTEXT,
        )


def test_real_adapter_prepares_unlisted_tool_only_with_signed_mapping():
    value = knowledge_mapping()
    signed = policy()
    signed['tool_name'] = 'test_read_counter'
    signed['mapping_hash'] = mapping_hash(value)
    real = MCPAdapter(
        None,
        ConnectionPolicy(
            [
                TrustedConnection(
                    'team',
                    'agent',
                    'local',
                    'read',
                    frozenset({'test_read_counter'}),
                    (GRANT,),
                    (signed,),
                )
            ]
        ),
    )
    mapped = real.prepare(
        'local',
        'read',
        'test_read_counter',
        {'query': 'help', 'scene': 'qa'},
        team_id='team',
        agent_id='agent',
        query_scope=SCOPE,
        correlation=CONTEXT,
        argument_mapping=value,
        full_schema={
            'type': 'object',
            'properties': {
                'query': {'type': 'string'},
                'scene': {'type': 'string'},
                'scope': {
                    'type': 'object',
                    'properties': {
                        'team': {'type': 'string'},
                        'environment': {'type': 'string'},
                        'region': {'type': 'string'},
                        'service': {'type': 'string'},
                        'source_id': {'type': 'string'},
                    },
                },
                'context': {
                    'type': 'object',
                    'properties': {
                        'run_id': {'type': 'string'},
                        'invocation_id': {'type': 'string'},
                        'query_scope_ref': {'type': 'string'},
                    },
                },
            },
            'required': ['query', 'scene', 'scope', 'context'],
        },
    )

    assert mapped['scope'] == {
        'team': 'ExampleTeam',
        'environment': 'test',
        'region': 'id',
        'service': 'checkout',
        'source_id': 'kb',
    }
    assert mapped['context'] == CONTEXT


def test_real_adapter_rejects_unlisted_tool_without_mapping():
    with pytest.raises(PermissionError):
        adapter().prepare(
            'local',
            'read',
            'knowledge_search',
            {'query': 'help', 'scene': 'qa'},
            team_id='team',
            agent_id='agent',
            query_scope=SCOPE,
            correlation=CONTEXT,
        )


def test_adapter_rejects_signed_policy_when_mapping_has_changed():
    value = knowledge_mapping()
    signed = policy()
    signed['tool_name'] = 'test_read_counter'
    signed['mapping_hash'] = mapping_hash(value)
    real = MCPAdapter(
        None,
        ConnectionPolicy(
            [
                TrustedConnection(
                    'team',
                    'agent',
                    'local',
                    'read',
                    frozenset({'test_read_counter'}),
                    (GRANT,),
                    (signed,),
                )
            ]
        ),
    )
    changed = knowledge_mapping().model_copy(
        update={
            'model_input_schema': {
                'type': 'object',
                'additionalProperties': False,
                'properties': {
                    'query': {'type': 'string'},
                    'scene': {'type': 'string'},
                    'limit': {'type': 'integer'},
                },
                'required': ['query', 'scene'],
            },
        }
    )
    with pytest.raises(PermissionError):
        real.prepare(
            'local',
            'read',
            'test_read_counter',
            {'query': 'help', 'scene': 'qa'},
            team_id='team',
            agent_id='agent',
            query_scope=SCOPE,
            correlation=CONTEXT,
            argument_mapping=changed,
            full_schema={
                'type': 'object',
                'properties': {
                    'query': {'type': 'string'},
                    'scene': {'type': 'string'},
                    'scope': {
                        'type': 'object',
                        'properties': {
                            'team': {'type': 'string'},
                            'environment': {'type': 'string'},
                            'region': {'type': 'string'},
                            'service': {'type': 'string'},
                            'source_id': {'type': 'string'},
                        },
                    },
                    'context': {
                        'type': 'object',
                        'properties': {
                            'run_id': {'type': 'string'},
                            'invocation_id': {'type': 'string'},
                            'query_scope_ref': {'type': 'string'},
                        },
                    },
                },
                'required': ['query', 'scene', 'scope', 'context'],
            },
        )


@pytest.mark.asyncio
async def test_declarative_mapping_pins_published_schema_against_drift():
    class Session:
        called = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def list_tools(self):
            from types import SimpleNamespace

            return SimpleNamespace(
                tools=[
                    {
                        'name': 'test_read_counter',
                        'inputSchema': {
                            'type': 'object',
                            'properties': {'query': {'type': 'integer'}},
                        },
                    }
                ],
                nextCursor=None,
            )

        async def call_tool(self, *args):
            self.called = True

    value = knowledge_mapping()
    signed = policy()
    signed['tool_name'] = 'test_read_counter'
    signed['mapping_hash'] = mapping_hash(value)
    real = MCPAdapter(
        None,
        ConnectionPolicy(
            [
                TrustedConnection(
                    'team',
                    'agent',
                    'local',
                    'read',
                    frozenset({'test_read_counter'}),
                    (GRANT,),
                    (signed,),
                )
            ]
        ),
    )
    real.session_factory = lambda *_: Session()
    from workbench_agent.tools.mcp import SchemaChanged

    with pytest.raises(SchemaChanged):
        await real.call(
            'local',
            'read',
            'test_read_counter',
            {'query': 'help', 'scene': 'qa'},
            team_id='team',
            agent_id='agent',
            expected_schema={
                'type': 'object',
                'properties': {
                    'query': {'type': 'string'},
                    'scene': {'type': 'string'},
                    'scope': {
                        'type': 'object',
                        'properties': {
                            'team': {'type': 'string'},
                            'environment': {'type': 'string'},
                            'region': {'type': 'string'},
                            'service': {'type': 'string'},
                            'source_id': {'type': 'string'},
                        },
                    },
                    'context': {
                        'type': 'object',
                        'properties': {
                            'run_id': {'type': 'string'},
                            'invocation_id': {'type': 'string'},
                            'query_scope_ref': {'type': 'string'},
                        },
                    },
                },
                'required': ['query', 'scene', 'scope', 'context'],
            },
            query_scope=SCOPE,
            correlation=CONTEXT,
            argument_mapping=value,
        )

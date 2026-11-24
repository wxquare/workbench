import json

import pytest
from pydantic import ValidationError

from workbench_agent.tools.scope_mapping import (
    ArgumentMapping,
    FieldMapping,
    bind_arguments,
    mapping_hash,
    validate_mapping,
)


SCOPE = {
    'service': 'checkout',
    'environment': 'test',
    'region': 'id',
    'start_at': '2026-11-20T00:00:00Z',
    'end_at': '2026-11-20T00:10:00Z',
}
GRANT = {
    'kind': 'dod-mcp-v1',
    'team': 'ExampleTeam',
    'allowed_scope': {
        'service': 'checkout',
        'environment': 'test',
        'region': 'id',
    },
    'earliest': '2026-11-20 00:00:00',
    'latest': '2026-11-20 23:59:59',
    'max_span_seconds': 3600,
    'timezone': 'Asia/Shanghai',
    'sources': {'knowledge_search': 'kb'},
}
CONTEXT = {
    'run_id': 'run-1',
    'invocation_id': 'invocation-1',
    'query_scope_ref': 'scope-1',
}
FULL_SCHEMA = {
    'type': 'object',
    'additionalProperties': False,
    'properties': {
        'query': {'type': 'string'},
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
    'required': ['query', 'scope', 'context'],
}


def mapping(*fields):
    return ArgumentMapping(
        fields=fields,
        model_input_schema={
            'type': 'object',
            'additionalProperties': False,
            'properties': {'query': {'type': 'string'}},
            'required': ['query'],
        },
    )


def knowledge_mapping():
    return mapping(
        FieldMapping(origin='model', source='query', target=('query',)),
        FieldMapping(origin='query_scope', source='environment', target=('scope', 'environment')),
        FieldMapping(origin='query_scope', source='region', target=('scope', 'region')),
        FieldMapping(origin='query_scope', source='service', target=('scope', 'service')),
        FieldMapping(origin='grant', source='team', target=('scope', 'team')),
        FieldMapping(
            origin='grant', source='sources.knowledge_search', target=('scope', 'source_id')
        ),
        FieldMapping(origin='context', source='run_id', target=('context', 'run_id')),
        FieldMapping(origin='context', source='invocation_id', target=('context', 'invocation_id')),
        FieldMapping(
            origin='context', source='query_scope_ref', target=('context', 'query_scope_ref')
        ),
    )


def policy(value=None):
    return {
        'tool_name': 'knowledge_search',
        'mapping_hash': mapping_hash(value or knowledge_mapping()),
        'model_input_fields': ['query'],
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


def test_valid_mapping_binds_only_declared_sources_and_has_stable_hash():
    value = knowledge_mapping()
    validate_mapping(value, FULL_SCHEMA, policy(value))
    from workbench_agent.tools.scope_mapping import mapping_fingerprint

    assert len(mapping_fingerprint(value, FULL_SCHEMA)) == 64

    result = bind_arguments(
        value,
        {'query': 'retry queue'},
        query_scope=SCOPE,
        grant=GRANT,
        context=CONTEXT,
    )

    assert result == {
        'query': 'retry queue',
        'scope': {
            'team': 'ExampleTeam',
            'environment': 'test',
            'region': 'id',
            'service': 'checkout',
            'source_id': 'kb',
        },
        'context': CONTEXT,
    }
    assert mapping_hash(value) == mapping_hash(knowledge_mapping())


@pytest.mark.parametrize(
    'model_args',
    [
        {'query': 'retry', 'scope': {'team': 'attacker'}},
        {'query': 'retry', 'context': CONTEXT},
        {'query': 'retry', 'team': 'attacker'},
        {'query': 'retry', 'credential': 'secret'},
    ],
)
def test_model_cannot_override_identity_or_undeclared_fields(model_args):
    with pytest.raises(PermissionError):
        bind_arguments(
            knowledge_mapping(),
            model_args,
            query_scope=SCOPE,
            grant=GRANT,
            context=CONTEXT,
        )


def test_mapping_rejects_duplicate_targets_and_parent_child_overlap():
    duplicate = knowledge_mapping().model_copy(
        update={
            'fields': (
                *knowledge_mapping().fields,
                FieldMapping(
                    origin='model',
                    source='query',
                    target=('query',),
                ),
            ),
        }
    )
    with pytest.raises(ValueError):
        validate_mapping(duplicate, FULL_SCHEMA, policy())

    overlap = mapping(
        FieldMapping(origin='model', source='query', target=('scope',)),
        FieldMapping(origin='grant', source='team', target=('scope', 'team')),
    )
    with pytest.raises(ValueError):
        validate_mapping(overlap, FULL_SCHEMA, policy())


def test_mapping_rejects_unknown_sources_bad_types_and_unpinned_hash():
    unknown = knowledge_mapping().model_copy(
        update={
            'fields': (
                FieldMapping(origin='grant', source='unknown', target=('scope', 'team')),
                *knowledge_mapping().fields[1:],
            ),
        }
    )
    with pytest.raises(ValueError):
        validate_mapping(unknown, FULL_SCHEMA, policy())

    bad_type = knowledge_mapping().model_copy(
        update={
            'fields': (
                FieldMapping(origin='model', source='query', target=('scope', 'region')),
                *knowledge_mapping().fields[1:],
            ),
        }
    )
    with pytest.raises(ValueError):
        validate_mapping(bad_type, FULL_SCHEMA, policy())

    wrong_policy = policy()
    wrong_policy['mapping_hash'] = '0' * 64
    with pytest.raises(ValueError):
        validate_mapping(knowledge_mapping(), FULL_SCHEMA, wrong_policy)


@pytest.mark.parametrize(
    'target',
    [
        ('scope', 0),
        ('scope', '*'),
        ('scope', 'team', 'nested', 'too', 'deep'),
        ('',),
    ],
)
def test_field_target_is_a_bounded_object_path(target):
    with pytest.raises(ValidationError):
        FieldMapping(origin='model', source='query', target=target)


def test_expression_text_is_plain_data():
    text = '${grant.team} AND {{ scope.region }}'
    value = knowledge_mapping().model_copy(
        update={
            'fields': (
                FieldMapping(origin='model', source='query', target=('query',)),
                *knowledge_mapping().fields[1:],
            ),
        }
    )
    result = bind_arguments(
        value,
        {'query': text},
        query_scope=SCOPE,
        grant=GRANT,
        context=CONTEXT,
    )
    assert result['query'] == text


def test_binding_legacy_json_is_unchanged_when_mapping_is_absent():
    from workbench_agent.application.idempotency import digest
    from workbench_agent.domain.tools import ToolBinding

    legacy = {
        'alias': 'knowledge.search',
        'provider': 'mcp',
        'tool_name': 'knowledge_search',
        'server': 'local',
        'credential_scope': 'read',
        'permission': 'knowledge.read',
        'input_schema': {'type': 'object'},
    }
    binding = ToolBinding.model_validate(
        {
            **legacy,
            'output_schema': None,
            'read_only': True,
            'timeout_seconds': 10,
            'max_output_bytes': 65536,
        }
    )
    dumped = binding.model_dump(mode='json')
    assert 'argument_mapping' not in dumped
    assert digest(dumped) == digest(
        {
            **legacy,
            'output_schema': None,
            'read_only': True,
            'timeout_seconds': 10.0,
            'max_output_bytes': 65536,
        }
    )


def test_mapping_hash_is_over_canonical_mapping_only():
    value = knowledge_mapping()
    expected = (
        __import__('hashlib')
        .sha256(
            json.dumps(
                value.model_dump(mode='json'),
                sort_keys=True,
                separators=(',', ':'),
            ).encode()
        )
        .hexdigest()
    )
    assert mapping_hash(value) == expected


def test_mapping_fingerprint_pins_both_mapping_and_full_schema():
    from workbench_agent.tools.scope_mapping import mapping_fingerprint

    first = mapping_fingerprint(knowledge_mapping(), FULL_SCHEMA)
    changed_schema = json.loads(json.dumps(FULL_SCHEMA))
    changed_schema['properties']['query']['type'] = 'number'
    changed = mapping_fingerprint(knowledge_mapping(), changed_schema)
    assert first != changed


def test_schema_hash_rejects_schema_drift_under_same_mapping():
    import hashlib

    value = knowledge_mapping()
    approved = policy(value)
    approved['schema_hash'] = hashlib.sha256(
        json.dumps(
            FULL_SCHEMA,
            sort_keys=True,
            separators=(',', ':'),
        ).encode()
    ).hexdigest()
    validate_mapping(value, FULL_SCHEMA, approved)
    changed = json.loads(json.dumps(FULL_SCHEMA))
    changed['properties']['query']['type'] = 'number'
    with pytest.raises(ValueError):
        validate_mapping(value, changed, approved)


@pytest.mark.parametrize(
    'name,args,expected_start,expected_end',
    [
        ('metrics_query', {'template_id': 'error_rate'}, True, True),
        ('logs_search', {'keyword': 'error'}, True, True),
        ('trace_get', {'trace_id': 'abc'}, True, True),
    ],
)
def test_legacy_and_declarative_mappings_have_identical_semantics(
    name, args, expected_start, expected_end
):
    from contract.test_dod_mcp_mapping import CONTEXT, GRANT, SCOPE
    from workbench_agent.tools.mcp import build_dod_arguments
    from workbench_agent.tools.scope_mapping import bind_arguments

    value = ArgumentMapping(
        fields=(
            *(
                FieldMapping(
                    origin='model',
                    source=key,
                    target=(key,),
                )
                for key in args
            ),
            FieldMapping(
                origin='query_scope', source='environment', target=('scope', 'environment')
            ),
            FieldMapping(origin='query_scope', source='region', target=('scope', 'region')),
            FieldMapping(origin='query_scope', source='service', target=('scope', 'service')),
            FieldMapping(origin='grant', source='team', target=('scope', 'team')),
            FieldMapping(origin='grant', source=f'sources.{name}', target=('scope', 'source_id')),
            FieldMapping(origin='grant', source='start', target=('start',)),
            FieldMapping(origin='grant', source='end', target=('end',)),
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
                key: {'type': ('string' if isinstance(args[key], str) else 'object')}
                for key in args
            },
            'required': list(args),
        },
    )
    declared = bind_arguments(
        value,
        args,
        query_scope=SCOPE,
        grant={
            **GRANT,
            'start': '2026-11-13T08:00:00+08:00',
            'end': '2026-11-13T08:10:00+08:00',
        },
        context=CONTEXT,
    )
    legacy = build_dod_arguments(name, args, SCOPE, GRANT, CONTEXT)

    assert declared['scope'] == legacy['scope']
    assert declared['context'] == legacy['context']
    assert ('start' in declared) is expected_start
    assert ('end' in declared) is expected_end

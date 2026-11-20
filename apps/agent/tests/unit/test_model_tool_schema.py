import pytest

from workbench_agent.tools.model_schema import project_model_schema, validate_model_schema


def test_knowledge_search_accepts_complex_published_schema_via_scalar_projection():
    published = {
        'type': 'object',
        'properties': {
            'query': {'type': 'string'},
            'scope': {
                'type': 'object',
                'properties': {'service': {'type': 'string'}},
            },
        },
    }
    projected = project_model_schema('knowledge_search', published)
    assert set(projected['properties']) == {'query', 'scene', 'limit'}
    validate_model_schema(projected)


def test_unknown_nested_schema_is_rejected_for_model_projection():
    with pytest.raises(ValueError):
        project_model_schema('unknown', {
            'type': 'object',
            'additionalProperties': False,
            'properties': {
                'nested': {'type': 'object', 'properties': {}},
            },
        })


def test_closed_scalar_schema_is_accepted():
    validate_model_schema({
        'type': 'object',
        'additionalProperties': False,
        'properties': {
            'query': {'type': 'string'},
            'limit': {'type': 'integer'},
        },
        'required': ['query'],
    })

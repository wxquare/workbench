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
    from jsonschema import Draft202012Validator

    validator = Draft202012Validator(projected)
    assert validator.is_valid({'query': 'help', 'scene': 'qa'})
    for protected in ('scope', 'team', 'env', 'session_key'):
        assert not validator.is_valid({'query': 'help', 'scene': 'qa', protected: 'injected'})


def test_unknown_nested_schema_is_rejected_for_model_projection():
    with pytest.raises(ValueError):
        project_model_schema(
            'unknown',
            {
                'type': 'object',
                'additionalProperties': False,
                'properties': {
                    'nested': {'type': 'object', 'properties': {}},
                },
            },
        )


def test_closed_scalar_schema_is_accepted():
    validate_model_schema(
        {
            'type': 'object',
            'additionalProperties': False,
            'properties': {
                'query': {'type': 'string'},
                'limit': {'type': 'integer'},
            },
            'required': ['query'],
        }
    )


def test_knowledge_expert_advertises_and_enforces_query_scene_enum():
    from langchain_core.utils.function_calling import convert_to_openai_tool
    from pydantic import ValidationError
    from workbench_agent.runtime.experts.knowledge import _schema_model

    published = {
        'type': 'object',
        'properties': {
            'query': {'type': 'string'},
            'scene': {'type': 'string', 'enum': ['qa', 'alert']},
            'scope': {'type': 'object'},
        },
    }
    schema = _schema_model('KnowledgeSearch', project_model_schema('knowledge_search', published))
    declaration = convert_to_openai_tool(schema)['function']['parameters']
    assert declaration['properties']['scene']['enum'] == ['qa', 'alert']
    assert 'scope' not in declaration['properties']
    schema.model_validate({'query': 'PENDING', 'scene': 'qa', 'tool_call_id': 'call-1'})
    with pytest.raises(ValidationError):
        schema.model_validate({'query': 'PENDING', 'scene': 'checkout', 'tool_call_id': 'call-2'})


def test_knowledge_projection_preserves_upstream_scene_restriction():
    from workbench_agent.runtime.experts.knowledge import _schema_model

    published = {
        'type': 'object',
        'properties': {
            'scene': {'type': 'string', 'enum': ['qa']},
        },
    }
    schema = _schema_model('QAOnlySearch', project_model_schema('knowledge_search', published))
    assert schema.model_json_schema()['properties']['scene'].get('const') == 'qa'

"""Shared safe projection of published MCP schemas for model tool declarations."""

from __future__ import annotations


SCALAR_TYPES = {'string', 'integer', 'number', 'boolean'}


def validate_model_schema(schema: dict) -> None:
    if (
        not isinstance(schema, dict)
        or schema.get('type') != 'object'
        or schema.get('additionalProperties', True) is not False
    ):
        raise ValueError('tool schema must be a closed object')
    properties = schema.get('properties', {})
    required = schema.get('required', [])
    if not isinstance(properties, dict) or not isinstance(required, list):
        raise ValueError('invalid model tool schema')
    for key, spec in properties.items():
        if (
            not isinstance(key, str)
            or not key.isidentifier()
            or key == 'tool_call_id'
            or not isinstance(spec, dict)
            or spec.get('type') not in SCALAR_TYPES
            or any(
                token in spec
                for token in ('$ref', 'properties', 'items', 'oneOf', 'anyOf', 'allOf')
            )
        ):
            raise ValueError(f'unsupported model tool schema field: {key}')
    if not set(required) <= set(properties):
        raise ValueError('invalid required schema')


def project_model_schema(tool_name: str, published_schema: dict) -> dict:
    if tool_name == 'knowledge_search':
        scene = published_schema.get('properties', {}).get('scene', {})
        return {
            'type': 'object',
            'properties': {
                'query': {'type': 'string'},
                'scene': {'type': 'string', 'enum': scene.get('enum', ['qa', 'alert'])},
                'limit': {'type': 'integer'},
            },
            'required': ['query', 'scene'],
            'additionalProperties': False,
        }
    validate_model_schema(published_schema)
    return published_schema

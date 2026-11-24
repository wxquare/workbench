"""Shared input and approval response validation for durable waits."""

INPUT_SCHEMA = {
    'type': 'object',
    'properties': {'region': {'type': 'string'}},
    'required': ['region'],
    'additionalProperties': False,
}


def validate_response(descriptor, response):
    """Exact registered v2 response schemas; unknown schema versions fail closed."""
    if descriptor['kind'] != response['kind']:
        return False
    if response['kind'] == 'approval':
        return descriptor['schema'] == {'type': 'approval-v1'}
    values = response['values']
    return (
        descriptor['schema'] == INPUT_SCHEMA
        and set(values) == {'region'}
        and isinstance(values['region'], str)
        and bool(values['region'])
    )

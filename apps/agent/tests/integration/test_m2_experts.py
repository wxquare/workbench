import pytest


def test_business_tool_schema_hides_scope_and_message_side_effect_fields():
    from workbench_agent.runtime.experts.diagnosis import model_tool_schema

    full = {
        'type': 'object',
        'properties': {
            'query': {'type': 'string'},
            'team': {'type': 'string'},
            'env': {'type': 'string'},
            'session_key': {'type': 'string'},
        },
        'additionalProperties': False,
    }
    exposed = model_tool_schema('SRE-Log-Query', full)
    assert set(exposed['properties']) == {'query', 'limit'}
    assert exposed['required'] == ['query']
    assert model_tool_schema('Common-DoD-GetInfo', full)['properties'] == {}
    with pytest.raises(ValueError):
        model_tool_schema('SRE-Log-TraceQuery', full)

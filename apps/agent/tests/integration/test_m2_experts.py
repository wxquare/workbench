import pytest

from workbench_agent.runtime.experts.catalog import ExpertRegistry, select_experts


def test_expert_registry_pins_versions_and_rejects_replacement():
    registry = ExpertRegistry()
    first, second = object(), object()
    registry.register('diagnosis', 'v1', first)
    registry.register('diagnosis', 'v2', second)
    assert registry.resolve('diagnosis', 'v1') is first
    with pytest.raises(ValueError):
        registry.register('diagnosis', 'v1', second)
    with pytest.raises(LookupError):
        registry.resolve('diagnosis', 'v3')


def test_expert_selection_is_bounded_and_rejects_unpublished_model_choices():
    assert select_experts('code', {'knowledge', 'code'}) == ('knowledge', 'code')
    assert select_experts('qa', {'knowledge'}) == ('knowledge',)
    assert select_experts('auto', {'knowledge'}, classifier_choice='code') == ()
    assert select_experts('auto', {'knowledge'}, classifier_choice='qa') == ()
    assert select_experts('auto', {'knowledge'}, classifier_choice='qa', published_modes={'qa'}) == ('knowledge',)
    assert len(select_experts('rca', {'diagnosis', 'knowledge', 'code'})) <= 2


def test_business_tool_schema_hides_scope_and_message_side_effect_fields():
    from workbench_agent.runtime.experts.diagnosis import model_tool_schema
    full = {'type': 'object', 'properties': {'query': {'type': 'string'}, 'team': {'type': 'string'},
        'env': {'type': 'string'}, 'session_key': {'type': 'string'}}, 'additionalProperties': False}
    exposed = model_tool_schema('SRE-Log-Query', full)
    assert set(exposed['properties']) == {'query', 'limit'}
    assert exposed['required'] == ['query']
    assert model_tool_schema('Common-DoD-GetInfo', full)['properties'] == {}
    with pytest.raises(ValueError):
        model_tool_schema('SRE-Log-TraceQuery', full)

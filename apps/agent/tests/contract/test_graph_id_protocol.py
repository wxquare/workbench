"""The wire protocol exposes graph IDs without accepting the old name."""
from dataclasses import asdict
import pytest
from pydantic import ValidationError

from workbench_agent.domain.profiles import ProfileDraft, VersionRef
from workbench_agent.domain.guards import WorkerCapabilities
from workbench_agent.settings import Settings


def test_profile_uses_only_graph_id():
    ref = VersionRef(resource_id='model', version='1', content_hash='a' * 64)
    draft = ProfileDraft(default_model=ref, graph_id='conversation.multi_turn')
    assert draft.model_dump()['graph_id'] == 'conversation.multi_turn'
    assert 'graph_version' not in draft.model_dump()
    with pytest.raises(ValidationError):
        ProfileDraft(default_model=ref, graph_version='dod.v18')


def test_worker_capabilities_use_graph_ids(config_data):
    capability = WorkerCapabilities(graph_ids=('conversation.multi_turn',))
    assert asdict(capability)['graph_ids'] == ('conversation.multi_turn',)
    assert 'graph_versions' not in asdict(capability)
    assert set(Settings.model_validate(config_data).worker.supported_graph_ids) == {
        'conversation.single_turn', 'conversation.multi_turn'}


async def test_configured_worker_registry_exposes_only_enabled_graphs(platform):
    from workbench_agent.runtime.graphs.registry import foundation_worker_registry
    registry = foundation_worker_registry(platform.backend, supported_graph_ids=platform.settings.worker.supported_graph_ids)
    assert set(registry.capabilities().graph_ids) == {'conversation.single_turn', 'conversation.multi_turn'}
    with pytest.raises(ValueError):
        foundation_worker_registry(platform.backend, supported_graph_ids=['retired.graph'])

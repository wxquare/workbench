"""The worker exposes only supported graphs and new agents get a usable default."""
import pytest

from workbench_agent.runtime.graphs.registry import foundation_worker_registry


async def test_only_retained_graphs_are_installed(platform):
    registry = foundation_worker_registry(platform.backend)
    assert set(registry.capabilities().graph_ids) == {'sop.conditional', 'rca.synthetic', 'rca.proposal', 'rca.batch_proposal', 'knowledge.scoped_excerpt', 'conversation.single_turn', 'conversation.multi_turn', 'knowledge.citation_review'}
    graphs = next(iter(registry._executors.values())).registry
    for number in range(1, 12):
        with pytest.raises(ValueError):
            graphs.state_schema_version(f'dod.v{number}')


async def test_new_agent_defaults_to_multi_turn_qa(platform):
    created = await platform.profiles.create_agent(platform.auth, 'New agent', key='new-graph-default')
    agent = await platform.profiles.get_agent(platform.auth, created['agent_id'])
    assert agent['draft']['graph_id'] == 'conversation.multi_turn'
    assert agent['draft']['allowed_modes'] == ['qa']

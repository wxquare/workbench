"""Installed graph identities are semantic and retired identities cannot resolve."""

import pytest

from workbench_agent.runtime.graphs.ids import ALL_GRAPH_IDS
from workbench_agent.runtime.graphs.registry import foundation_worker_registry


async def test_registry_exposes_only_semantic_graph_ids(platform):
    expected = {
        'sop.conditional',
        'rca.synthetic',
        'rca.proposal',
        'rca.batch_proposal',
        'knowledge.scoped_excerpt',
        'conversation.single_turn',
        'conversation.multi_turn',
        'knowledge.citation_review',
        'conversation.tool_use',
    }
    registry = foundation_worker_registry(platform.backend)
    assert set(ALL_GRAPH_IDS) == expected
    assert set(registry.capabilities().graph_ids) == expected
    graphs = next(iter(registry._executors.values())).registry
    for number in range(1, 20):
        with pytest.raises(ValueError):
            graphs.state_schema_version(f'dod.v{number}')

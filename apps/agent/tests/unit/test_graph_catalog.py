"""The catalog describes every installed graph and its actual structure."""
from workbench_agent.runtime.graphs.catalog import list_graphs, get_graph, graph_structure


def test_catalog_covers_each_installed_graph_with_meaningful_metadata():
    specs = list_graphs()
    assert {item.graph_id for item in specs} == {
        'sop.conditional', 'rca.synthetic', 'rca.proposal', 'rca.batch_proposal',
        'knowledge.scoped_excerpt', 'conversation.single_turn', 'conversation.multi_turn',
        'knowledge.citation_review'}
    for spec in specs:
        assert spec.title and spec.one_liner and spec.when_to_use and spec.when_not_to_use and spec.steps_text
        assert spec.status in ('recommended', 'experimental')
        structure = graph_structure(spec.graph_id)
        assert {n['id'] for n in structure['nodes']} == set(spec.nodes) | {'__start__', '__end__'}
        assert all(not e['conditional'] or e['label'] for e in structure['edges'])
        assert all(n.title and n.summary and n.reads and n.writes for n in spec.nodes.values())


def test_entrypoints_and_sop_loop_are_explicit():
    for name in ('rca.synthetic', 'rca.proposal', 'rca.batch_proposal'):
        assert get_graph(name).capabilities.entrypoints == ('synthetic_alert',)
    assert get_graph('sop.conditional').capabilities.entrypoints == ('manual', 'schedule')
    for name in ('knowledge.scoped_excerpt', 'knowledge.citation_review', 'conversation.single_turn', 'conversation.multi_turn'):
        assert get_graph(name).capabilities.entrypoints == ('manual',)
    edges = graph_structure('sop.conditional')['edges']
    assert any(e['source'] == 'step' and e['target'] == 'step' and e['conditional'] and e['label'] for e in edges)

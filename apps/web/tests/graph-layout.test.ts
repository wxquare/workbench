import { describe, expect, it } from 'vitest'
import { layoutGraph } from '../src/graph-layout'

describe('graph layout', () => {
  it('lays out branches, isolated nodes and self loops deterministically', () => {
    const nodes = ['a', 'b', 'c', 'orphan'].map(id => ({ id }))
    const edges = [
      { source: 'a', target: 'b', conditional: true, label: '继续' },
      { source: 'b', target: 'b', conditional: true, label: '重复' },
      { source: 'b', target: 'c', conditional: false, label: '' },
    ]
    const layout = layoutGraph(nodes, edges)
    expect(layout).toEqual(layoutGraph([...nodes].reverse(), [...edges].reverse()))
    expect(new Set(layout.nodes.map(n => `${n.x},${n.y}`)).size).toBe(nodes.length)
    expect(layout.nodes.every(n => Number.isFinite(n.x) && Number.isFinite(n.y))).toBe(true)
    const loop = layout.edges.find(e => e.source === 'b' && e.target === 'b')!
    expect(loop.path).toContain('C')
    expect(loop.label).toBe('重复')
    expect(Number.isFinite(loop.labelX) && Number.isFinite(loop.labelY)).toBe(true)
  })
})

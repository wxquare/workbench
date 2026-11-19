import { describe, expect, it } from 'vitest'
import { layoutGraph } from '../src/graph-layout'

describe('graph layout', () => {
  it('keeps a single-column loop and its label within a compact canvas', () => {
    const layout = layoutGraph([{ id: 'step' }], [{ source: 'step', target: 'step', conditional: true, label: '还有未执行的步骤' }])
    const loop = layout.edges[0]
    expect(layout.width).toBeLessThanOrEqual(360)
    expect(loop.labelX).toBeLessThan(layout.width - 50)
  })

  it('routes a conditional edge around a skipped node', () => {
    const nodes = ['prepare', 'step', 'compose'].map(id => ({ id }))
    const edges = [
      { source: 'prepare', target: 'step', conditional: true, label: '存在步骤' },
      { source: 'prepare', target: 'compose', conditional: true, label: '没有可执行步骤' },
      { source: 'step', target: 'compose', conditional: true, label: '全部完成' },
    ]
    const layout = layoutGraph(nodes, edges)
    const step = layout.nodes.find(node => node.id === 'step')!
    const skip = layout.edges.find(edge => edge.source === 'prepare' && edge.target === 'compose')!
    expect(skip.path).toContain('C')
    expect(skip.labelX).toBeLessThan(step.x - 90)
    expect(skip.labelX - skip.label.length * 5.5).toBeGreaterThan(5)
    expect(skip.labelY).toBeLessThan(step.y - 30)
  })

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

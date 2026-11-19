import type { GraphEdge } from './graphs-api'

type Node = { id: string }
type Positioned = { id: string; x: number; y: number }
type Routed = GraphEdge & { path: string; labelX: number; labelY: number }

/** Stable layers based on dependencies. Self loops never increase a node's layer. */
export function layoutGraph(nodes: Node[], edges: GraphEdge[]): { nodes: Positioned[]; edges: Routed[]; width: number; height: number } {
  const ids = [...new Set(nodes.map(node => node.id))].sort()
  const set = new Set(ids)
  const sortedEdges = edges.filter(e => set.has(e.source) && set.has(e.target))
    .slice().sort((a, b) => `${a.source}\0${a.target}\0${a.label}`.localeCompare(`${b.source}\0${b.target}\0${b.label}`))
  const incoming = new Map(ids.map(id => [id, 0])), layer = new Map(ids.map(id => [id, 0]))
  for (const edge of sortedEdges) if (edge.source !== edge.target) incoming.set(edge.target, incoming.get(edge.target)! + 1)
  const ready = ids.filter(id => incoming.get(id) === 0)
  while (ready.length) {
    const source = ready.shift()!
    for (const edge of sortedEdges.filter(e => e.source === source && e.target !== source)) {
      layer.set(edge.target, Math.max(layer.get(edge.target)!, layer.get(source)! + 1))
      incoming.set(edge.target, incoming.get(edge.target)! - 1)
      if (incoming.get(edge.target) === 0) { ready.push(edge.target); ready.sort() }
    }
  }
  const groups = new Map<number, string[]>()
  for (const id of ids) { const depth = layer.get(id)!; groups.set(depth, [...(groups.get(depth) ?? []), id]) }
  const width = Math.max(300, ...[...groups.values()].map(group => group.length * 240 + 80))
  const positioned = ids.map(id => {
    const depth = layer.get(id)!, group = groups.get(depth)!, index = group.indexOf(id)
    return { id, x: width / 2 + (index - (group.length - 1) / 2) * 240, y: 55 + depth * 140 }
  })
  const locations = new Map(positioned.map(node => [node.id, node]))
  const routed = sortedEdges.map(edge => {
    const a = locations.get(edge.source)!, b = locations.get(edge.target)!
    if (a.id === b.id) return { ...edge, path: `M ${a.x + 90} ${a.y} C ${a.x + 170} ${a.y - 65}, ${a.x + 170} ${a.y + 65}, ${a.x + 90} ${a.y + 22}`,
      labelX: a.x + 145, labelY: a.y - 40 }
    return { ...edge, path: `M ${a.x} ${a.y + 30} L ${b.x} ${b.y - 30}`, labelX: (a.x + b.x) / 2 + 8, labelY: (a.y + b.y) / 2 }
  })
  return { nodes: positioned, edges: routed, width: width + 110, height: Math.max(130, ...positioned.map(n => n.y + 55)) }
}

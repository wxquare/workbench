export interface GraphCapabilities {
  modes: string[]; entrypoints: string[]; uses_tools: boolean; uses_history: boolean
  needs_knowledge_tool: boolean; needs_workflow: boolean; may_interrupt: boolean; verified_answers: boolean
}
export interface GraphSummary {
  graph_id: string; title: string; summary: string; status: 'recommended' | 'experimental'
  capabilities: GraphCapabilities; agents_using: number
}
export interface GraphNode {
  id: string; title: string; kind: string; summary: string; reads: string[]; writes: string[]; cost: string[]
}
export interface GraphEdge { source: string; target: string; conditional: boolean; label: string }
export interface GraphDetail extends GraphSummary {
  when_to_use: string; when_not_to_use: string; notes: string[]; steps_text: string[]
  nodes: GraphNode[]; edges: GraphEdge[]
}
interface RequestClient { request<T>(path: string): Promise<T> }
export class GraphApi {
  constructor(private client: RequestClient) {}
  async list(): Promise<GraphSummary[]> { return (await this.client.request<{items: GraphSummary[]}>('/v1/graphs')).items }
  detail(id: string): Promise<GraphDetail> { return this.client.request(`/v1/graphs/${encodeURIComponent(id)}`) }
}

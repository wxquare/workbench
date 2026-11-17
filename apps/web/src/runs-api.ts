import type { ApiClient } from './api'

export interface RunItem {
  run_id: string
  thread_id: string
  agent_id: string
  status: string
  phase: string | null
  title: string
  mode: string | null
  created_at: string
}
export interface RunPage { items: RunItem[]; next_cursor: string | null }
export function listRuns(client: ApiClient, agent: string, options: { query?: string; status?: string; after?: string } = {}): Promise<RunPage> {
  const params = new URLSearchParams({ agent_id: agent, limit: '20' })
  if (options.after) params.set('after', options.after)
  if (options.status) params.set('status', options.status)
  if (options.query) params.set('q', options.query)
  return client.request(`/v1/runs?${params}`)
}

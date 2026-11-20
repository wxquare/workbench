import type { ApiClient } from './api'

export interface StatisticsSummary {
  generated_at: string
  definitions_version: 'stats-v1'
  cohort: { start_at: string; end_at: string; agent_id: string | null }
  counts: {
    runs: number
    active: number
    terminal: number
    succeeded: number
    partial: number
    state_inconsistent: number
  }
  rates: { success: number | null; partial: number | null }
  failure_top5: [string, number][]
  latency: { average_ms: number | null }
  costs: { known: Record<string, number>; pending_run_count: number }
  gaps: { open: number; resolved: number; dismissed: number }
  feedback: { negative_rate: number | null; subjects: number }
  trend: { day: string; runs: number; succeeded: number; partial: number }[]
}

export function getStatistics(
  client: ApiClient,
  startAt: string,
  endAt: string,
  agentId?: string,
): Promise<StatisticsSummary> {
  const query = new URLSearchParams({start_at: startAt, end_at: endAt})
  if (agentId) query.set('agent_id', agentId)
  return client.request(`/v1/stats/summary?${query}`)
}

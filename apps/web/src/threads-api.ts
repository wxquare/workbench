import type { ApiClient, RunOutcome } from './api'

export interface ThreadItem { thread_id: string; agent_id: string; title: string; status: string | null; last_run_id: string | null; updated_at: string; created_at: string }
export interface ThreadMessage { message_id: string; run_id: string | null; role: string; input: { text: string } | null; outcome: RunOutcome | null; created_at: string }
export interface Page<T> { items: T[]; next_cursor: string | null }

export function listThreads(client: ApiClient, agent: string, after?: string): Promise<Page<ThreadItem>> {
  const params = new URLSearchParams({ agent_id: agent, limit: '20' })
  if (after) params.set('after', after)
  return client.request(`/v1/threads?${params}`)
}
export function listMessages(client: ApiClient, thread: string, after?: string): Promise<Page<ThreadMessage>> {
  const params = new URLSearchParams({ limit: '100' })
  if (after) params.set('after', after)
  return client.request(`/v1/threads/${encodeURIComponent(thread)}/messages?${params}`)
}
export function submitConversation(client: ApiClient, agent: string, thread: string | null, text: string, key: string): Promise<{ run_id: string; thread_id: string }> {
  return client.request('/v1/runs', { method: 'POST', headers: { 'Content-Type': 'application/json', 'Idempotency-Key': key },
    body: JSON.stringify({ agent_id: agent, thread_id: thread, mode: 'qa', input: { text }, business_context: { conversation: true } }) })
}

import type { ApiClient } from './api'

export interface FeedbackView {
  feedback_id: string
  run_id: string
  rating: 1 | -1
  reason: 'irrelevant' | 'citation_error' | 'stale' | 'other' | null
  comment: string | null
  knowledge_gap_id: string | null
  review_status: string
}

export interface FeedbackSubmission {
  rating: 1 | -1
  reason?: 'irrelevant' | 'citation_error' | 'stale' | 'other'
  comment?: string
}

export function submitFeedback(
  client: ApiClient,
  runId: string,
  body: FeedbackSubmission,
  key: string,
): Promise<FeedbackView> {
  return client.request(`/v1/runs/${encodeURIComponent(runId)}/feedback`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json', 'Idempotency-Key': key},
    body: JSON.stringify(body),
  })
}

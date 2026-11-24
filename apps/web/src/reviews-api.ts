import type { ApiClient } from './api'

export const REVIEW_TYPES = [
  'run_input',
  'run_approval',
  'action_approval',
  'action_reapproval',
  'action_reconcile',
] as const
export type ReviewType = (typeof REVIEW_TYPES)[number]
export type ReviewOperation =
  | 'resume'
  | 'approve'
  | 'reject'
  | 'retry'
  | 'reapproval'
  | 'reconcile'

export interface ReviewItem {
  item_id: string
  type: ReviewType
  agent_id: string
  run_id: string
  action_id: string | null
  title: string
  due_at: string | null
  created_at: string
  expired: boolean
  available_operations: ReviewOperation[]
  simulated: boolean
  source_label: string
  links: { run: string }
}

export interface ReviewPage {
  items: ReviewItem[]
  next_cursor: string | null
  as_of: string
}

export interface ReviewSummary {
  total: number
  by_type: Record<ReviewType, number>
  as_of: string
}

export interface ReviewDetail {
  item: ReviewItem
  detail: Record<string, unknown>
}

export function listReviews(
  client: ApiClient,
  options: {
    type?: ReviewType
    state?: 'pending' | 'expired'
    limit?: number
    after?: string
  } = {},
): Promise<ReviewPage> {
  const query = new URLSearchParams()
  if (options.type) query.set('type', options.type)
  if (options.state) query.set('state', options.state)
  if (options.limit) query.set('limit', String(options.limit))
  if (options.after) query.set('after', options.after)
  return client.request(`/v1/reviews?${query}`)
}

export function getReviewSummary(client: ApiClient): Promise<ReviewSummary> {
  return client.request('/v1/reviews/summary')
}

export function getReview(client: ApiClient, itemId: string): Promise<ReviewDetail> {
  return client.request(
    `/v1/reviews/${encodeURIComponent(itemId)}`,
  )
}

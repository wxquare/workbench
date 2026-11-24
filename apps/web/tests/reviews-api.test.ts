import { expect, it, vi } from 'vitest'
import type { ApiClient } from '../src/api'
import {
  getReview,
  getReviewSummary,
  listReviews,
} from '../src/reviews-api'

function client() {
  return { request: vi.fn().mockResolvedValue({}) } as unknown as ApiClient & {
    request: ReturnType<typeof vi.fn>
  }
}

it('lists reviews with filter, state and cursor', async () => {
  const api = client()
  await listReviews(api, {
    type: 'action_approval',
    state: 'expired',
    limit: 20,
    after: 'cursor',
  })
  expect(api.request.mock.calls[0][0]).toBe(
    '/v1/reviews?type=action_approval&state=expired&limit=20&after=cursor',
  )
})

it('uses separate summary and encoded item routes', async () => {
  const api = client()
  await getReviewSummary(api)
  await getReview(api, 'run_input:interrupt-1')
  expect(api.request.mock.calls[0][0]).toBe('/v1/reviews/summary')
  expect(api.request.mock.calls[1][0]).toBe(
    '/v1/reviews/run_input%3Ainterrupt-1',
  )
})

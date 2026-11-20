import {expect, it, vi} from 'vitest'
import {submitFeedback} from '../src/feedback-api'

it('retries an uncertain feedback request with the same idempotency key', async () => {
  const request = vi.fn()
    .mockRejectedValueOnce(new TypeError('network'))
    .mockResolvedValueOnce({feedback_id: 'f', run_id: 'r', rating: -1, knowledge_gap_id: 'g'})
  const client = {request} as never
  const key = 'stable'
  await expect(submitFeedback(client, 'r', {rating: -1, reason: 'other'}, key)).rejects.toThrow()
  await submitFeedback(client, 'r', {rating: -1, reason: 'other'}, key)
  expect(request.mock.calls[0][1].headers['Idempotency-Key']).toBe(key)
  expect(request.mock.calls[1][1].headers['Idempotency-Key']).toBe(key)
})

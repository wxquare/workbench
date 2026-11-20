import {expect, it, vi} from 'vitest'
import {getStatistics} from '../src/statistics-api'

it('passes the explicit range and agent scope to the stats API', async () => {
  const request = vi.fn().mockResolvedValue({counts: {runs: 1}})
  await getStatistics({request} as never, '2026-11-20T00:00:00Z', '2026-11-21T00:00:00Z', 'agent')
  const path = request.mock.calls[0][0]
  expect(path).toContain('start_at=2026-11-20T00%3A00%3A00Z')
  expect(path).toContain('agent_id=agent')
})

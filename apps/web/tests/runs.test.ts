import { expect, it, vi } from 'vitest'
import { ApiClient } from '../src/api'
import { listRuns } from '../src/runs-api'

it('requests an agent-scoped filtered run page with bearer authentication', async () => {
  const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json({ items: [], next_cursor: null }))
  const client = new ApiClient(() => 'token', fetcher)
  await listRuns(client, 'agent', { query: 'hello world', status: 'succeeded', after: 'cursor' })
  expect(fetcher.mock.calls[0][0]).toBe('/v1/runs?agent_id=agent&limit=20&after=cursor&status=succeeded&q=hello+world')
  expect(new Headers(fetcher.mock.calls[0][1]?.headers).get('Authorization')).toBe('Bearer token')
})

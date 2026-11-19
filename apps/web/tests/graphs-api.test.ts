import { describe, expect, it, vi } from 'vitest'
import { ApiClient, ApiError } from '../src/api'
import { GraphApi } from '../src/graphs-api'

describe('graph catalog client', () => {
  it('reads the list and encodes detail identifiers', async () => {
    const fetcher = vi.fn().mockResolvedValueOnce(Response.json({ items: [{ graph_id: 'conversation.multi_turn' }] }))
      .mockResolvedValueOnce(Response.json({ graph_id: 'graph/a' }))
    const api = new GraphApi(new ApiClient(() => 'token', fetcher))
    expect(await api.list()).toEqual([{ graph_id: 'conversation.multi_turn' }])
    expect((await api.detail('graph/a')).graph_id).toBe('graph/a')
    expect(fetcher.mock.calls[1][0]).toBe('/v1/graphs/graph%2Fa')
  })
  it('propagates authentication and not-found errors', async () => {
    const api = new GraphApi(new ApiClient(() => 'token', vi.fn().mockResolvedValue(Response.json({ code: 'NOT_FOUND' }, { status: 404 }))))
    await expect(api.detail('unknown')).rejects.toMatchObject({ status: 404 })
    await expect(new GraphApi(new ApiClient(() => '')).list()).rejects.toBeInstanceOf(ApiError)
  })
})

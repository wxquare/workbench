import { describe, expect, it, vi } from 'vitest'
import { ApiClient } from '../src/api'
import { createModelsApi } from '../src/models-api'

describe('models API', () => {
  it('sends model credentials only to the dedicated endpoint', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValueOnce(Response.json({ revision: 4 }))
    const models = createModelsApi(new ApiClient(() => 'token', fetcher))
    await models.setCredential('my model', 'fictional-key', 3)
    expect(fetcher.mock.calls[0][0]).toBe('/v1/model-profiles/my%20model/credential')
    expect(fetcher.mock.calls[0][1]?.method).toBe('PUT')
    expect(new Headers(fetcher.mock.calls[0][1]?.headers).get('If-Match')).toBe('3')
    expect(JSON.parse(fetcher.mock.calls[0][1]?.body as string)).toEqual({ api_key: 'fictional-key' })
  })
  it('pages through the team model library', async () => {
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(Response.json({ items: [{ name: 'first' }], next_cursor: 'a' }))
      .mockResolvedValueOnce(Response.json({ items: [{ name: 'second' }], next_cursor: null }))
    const models = createModelsApi(new ApiClient(() => 'token', fetcher))
    expect((await models.list()).map(item => item.name)).toEqual(['first', 'second'])
    expect(fetcher.mock.calls[1][0]).toBe('/v1/model-profiles?limit=100&after=a')
  })

  it('stops if the server repeats a pagination cursor', async () => {
    const fetcher = vi.fn<typeof fetch>().mockImplementation(async () => Response.json({ items: [], next_cursor: 'a' }))
    const models = createModelsApi(new ApiClient(() => 'token', fetcher))
    await expect(models.list()).rejects.toThrow('游标')
    expect(fetcher).toHaveBeenCalledTimes(2)
  })

  it('reuses keys on uncertain retries and sends the revision on edits and publish', async () => {
    const fetcher = vi.fn<typeof fetch>().mockRejectedValueOnce(new TypeError('network'))
      .mockResolvedValueOnce(Response.json({ revision: 2 }))
      .mockResolvedValueOnce(Response.json({ version: '1' }))
    const keys = ['patch-key', 'publish-key']
    const models = createModelsApi(new ApiClient(() => 'token', fetcher), () => keys.shift()!)
    await expect(models.patch('model', { secret_ref: 'env://MODEL_KEY' }, 1)).rejects.toThrow('network')
    await models.patch('model', { secret_ref: 'env://MODEL_KEY' }, 1)
    await models.publish('model', 2)
    const headers = fetcher.mock.calls.map(call => new Headers(call[1]?.headers))
    expect(headers.map(h => h.get('Idempotency-Key'))).toEqual(['patch-key', 'patch-key', 'publish-key'])
    expect(headers.map(h => h.get('If-Match'))).toEqual(['1', '1', '2'])
  })

  it('rejects plaintext secrets before making a network request', async () => {
    const fetcher = vi.fn<typeof fetch>()
    const models = createModelsApi(new ApiClient(() => 'token', fetcher))
    await expect(models.create({ name: 'model', secret_ref: 'plaintext-key' })).rejects.toThrow()
    expect(fetcher).not.toHaveBeenCalled()
  })

  it('rejects endpoint URLs that could carry credentials before sending them', async () => {
    const fetcher = vi.fn<typeof fetch>()
    const models = createModelsApi(new ApiClient(() => 'token', fetcher))
    await expect(models.create({ name: 'model', base_url: 'https://user:password@example.com' })).rejects.toThrow()
    await expect(models.create({ name: 'model', base_url: 'https://example.com?token=secret' })).rejects.toThrow()
    expect(fetcher).not.toHaveBeenCalled()
  })

  it('creates and probes team models, then fetches the job through its chosen agent', async () => {
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(Response.json({ name: 'my model' }))
      .mockResolvedValueOnce(Response.json({ job_id: 'job', agent_id: 'chosen-agent', status: 'queued', result_ref: null }))
      .mockResolvedValueOnce(Response.json({ job_id: 'job', status: 'succeeded', result_ref: '{"configuration":"ok"}' }))
    const models = createModelsApi(new ApiClient(() => 'token', fetcher), () => 'key')
    await models.create({ name: 'my model', secret_ref: 'secret://team/key' })
    const probe = await models.probe('my model')
    expect((await models.job(probe.agent_id, 'job')).status).toBe('succeeded')
    expect(fetcher.mock.calls.map(call => call[0])).toEqual([
      '/v1/model-profiles', '/v1/model-profiles/my%20model/probe', '/v1/agents/chosen-agent/admin-jobs/job',
    ])
    expect(JSON.parse(fetcher.mock.calls[0][1]?.body as string)).toEqual({ name: 'my model', secret_ref: 'secret://team/key' })
  })
})

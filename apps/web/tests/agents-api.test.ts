import { describe, expect, it, vi } from 'vitest'
import { ApiClient } from '../src/api'
import { AgentApi, parseAgentDraft, agentError } from '../src/agents-api'

const draft = {
  default_model: { resource_id: 'model', version: '1', content_hash: 'a'.repeat(64) },
  graph_id: 'conversation.multi_turn', role_models: [], skills: [{ resource_id: 'skill', version: '2', content_hash: 'b'.repeat(64) }],
  tools: [], repositories: [], budget_limits: { tokens: 500 }, allowed_modes: ['qa'],
}

describe('Agent API', () => {
  it('saves the complete draft using the current revision and authentication', async () => {
    const fetcher = vi.fn().mockResolvedValue(Response.json({ agent_id: 'ws', revision: 8, draft }))
    const client = new AgentApi(new ApiClient(() => 'token', fetcher), () => 'save-key')
    const edited = parseAgentDraft(JSON.stringify({ ...draft, graph_id: 'sop.conditional' }))
    await client.save('ws', 7, edited)
    const [path, init] = fetcher.mock.calls[0]
    expect(path).toBe('/v1/agents/ws')
    expect(init.method).toBe('PATCH')
    expect(new Headers(init.headers).get('If-Match')).toBe('7')
    expect(new Headers(init.headers).get('Idempotency-Key')).toBe('save-key')
    expect(new Headers(init.headers).get('Authorization')).toBe('Bearer token')
    expect(JSON.parse(init.body)).toEqual({ ...draft, graph_id: 'sop.conditional' })
  })

  it('reuses the idempotency key after an uncertain failure and changes it for a different operation', async () => {
    const fetcher = vi.fn().mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce(Response.json({ agent_id: 'ws', revision: 1, draft }))
      .mockResolvedValueOnce(Response.json({ profile_id: 'p', version: '1' }, { status: 201 }))
    let key = 0
    const client = new AgentApi(new ApiClient(() => 'token', fetcher), () => `key-${++key}`)
    await expect(client.save('ws', 0, draft)).rejects.toThrow('offline')
    await client.save('ws', 0, draft)
    await client.publish('ws', 1)
    const headers = fetcher.mock.calls.map(([, init]) => new Headers(init.headers))
    expect(headers.map(h => h.get('Idempotency-Key'))).toEqual(['key-1', 'key-1', 'key-2'])
    expect(fetcher.mock.calls[2][0]).toBe('/v1/agents/ws/profiles/publish')
    expect(headers[2].get('If-Match')).toBe('1')
  })

  it('creates an agent and reads its actual draft', async () => {
    const fetcher = vi.fn().mockResolvedValueOnce(Response.json({ agent_id: 'new', name: 'New', revision: 0 }, { status: 201 }))
      .mockResolvedValueOnce(Response.json({ agent_id: 'new', name: 'New', revision: 0, active_profile_id: null, draft }))
    const client = new AgentApi(new ApiClient(() => 'token', fetcher), () => 'create-key')
    await client.create('New')
    expect(JSON.parse(fetcher.mock.calls[0][1].body)).toEqual({ name: 'New' })
    expect((await client.get('new')).draft).toEqual(draft)
  })

  it('loads real active model refs across pages without inventing refs for unpublished models', async () => {
    const active = { resource_id: 'version-id', version: '2', content_hash: 'c'.repeat(64) }
    const fetcher = vi.fn().mockResolvedValueOnce(Response.json({ items: [{ name: 'first', active_ref: active }], next_cursor: 'cursor' }))
      .mockResolvedValueOnce(Response.json({ items: [{ name: 'unpublished', active_ref: null }], next_cursor: null }))
    const client = new AgentApi(new ApiClient(() => 'token', fetcher))
    expect(await client.models('ws')).toEqual([{ name: 'first', active_ref: active }])
    expect(fetcher.mock.calls[1][0]).toBe('/v1/agents/ws/model-profiles?limit=100&after=cursor')
  })

  it('rejects invalid draft JSON before any request', () => {
    expect(() => parseAgentDraft('{')).toThrow()
    expect(() => parseAgentDraft('[]')).toThrow()
    expect(() => parseAgentDraft('{}')).toThrow()
  })

  it('preserves omitted fields, including nested settings, while allowing explicit clears', () => {
    const original = { ...draft, budget_limits: { tokens: 500, tool_calls: 10 } }
    expect(parseAgentDraft('{"graph_id":"sop.conditional","budget_limits":{"tokens":100},"skills":[]}', original))
      .toEqual({ ...original, graph_id: 'sop.conditional', budget_limits: { tokens: 100, tool_calls: 10 }, skills: [] })
    expect(parseAgentDraft('{"budget_limits":null}', original).budget_limits).toBeNull()
  })

  it('distinguishes permission and revision errors from other conflicts', async () => {
    const fetcher = vi.fn().mockResolvedValueOnce(Response.json({ code: 'REVISION_CONFLICT' }, { status: 409 }))
      .mockResolvedValueOnce(Response.json({ code: 'DRAFT_REQUIRED' }, { status: 409 }))
      .mockResolvedValueOnce(Response.json({ code: 'OPERATION_DENIED' }, { status: 403 }))
    const client = new AgentApi(new ApiClient(() => 'token', fetcher), () => 'key')
    for (const expected of ['版本冲突', 'DRAFT_REQUIRED', '权限']) {
      try { await client.publish('ws', 0); throw new Error('expected failure') }
      catch (error) { expect(agentError(error)).toContain(expected) }
    }
  })
})

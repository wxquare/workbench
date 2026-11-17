import { describe, expect, it, vi } from 'vitest'
import { ApiClient, ApiError, agentName, createQaSubmission, isRunActive, missingQaFields, validQaWindow } from '../src/api'

const scope = { service: 'checkout', environment: 'test', region: 'id', start_at: '2026-11-16T01:00:00Z', end_at: '2026-11-16T02:00:00Z' }

describe('real QA API', () => {
  it('resolves the agent for a run from its agent identity and falls back to the id', () => {
    const agents = [{ agent_id: 'agent-1', name: '对话助手' }, { agent_id: 'agent-2', name: '分析助手' }]
    expect(agentName('agent-2', agents)).toBe('分析助手')
    expect(agentName('unknown', agents)).toBe('unknown')
  })

  it('lists missing required fields, including whitespace-only values', () => {
    expect(missingQaFields({ agent: 'ws', question: '', service: '  ', region: '', start: '', end: '' }))
      .toEqual(['问题', '服务', '区域', '开始时间', '结束时间'])
    expect(missingQaFields({ agent: 'ws', question: 'Why?', service: 'svc', region: 'id', start: '2026-11-16T10:00', end: '2026-11-16T10:10' })).toEqual([])
  })

  it('requires only an agent and question for pure LLM QA', () => {
    const fields = { agent: 'ws', question: 'Why?', service: '', region: '', start: '', end: '' }
    expect(missingQaFields(fields, 'llm')).toEqual([])
    expect(missingQaFields({ ...fields, question: '  ' }, 'llm')).toEqual(['问题'])
    expect(missingQaFields(fields, 'knowledge')).toEqual(['服务', '区域', '开始时间', '结束时间'])
  })

  it('calls the default fetch without binding it to the API client', async () => {
    let receiver: unknown
    vi.stubGlobal('fetch', function (this: unknown) {
      receiver = this
      return Promise.resolve(Response.json({ items: [], next_cursor: null }))
    })
    try {
      const client = new ApiClient(() => 'token')
      await client.agents()
      expect(receiver).not.toBe(client)
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('uses the supplied bearer identity and follows agent pagination', async () => {
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(Response.json({ items: [{ agent_id: 'one', name: 'One' }], next_cursor: 'one' }))
      .mockResolvedValueOnce(Response.json({ items: [{ agent_id: 'two', name: 'Two' }], next_cursor: null }))
    const client = new ApiClient(() => 'actual-token', fetcher)
    expect(await client.agents()).toHaveLength(2)
    expect(fetcher.mock.calls[1][0]).toBe('/v1/agents?limit=100&after=one')
    expect(new Headers(fetcher.mock.calls[0][1]?.headers).get('Authorization')).toBe('Bearer actual-token')
  })

  it('submits the five-field scope and reuses the idempotency key after an uncertain failure', async () => {
    const fetcher = vi.fn<typeof fetch>().mockRejectedValueOnce(new TypeError('network')).mockResolvedValueOnce(Response.json({ run_id: 'run-1' }, { status: 202 }))
    const client = new ApiClient(() => 'token', fetcher)
    const submit = createQaSubmission(client, () => 'stable-key')
    await expect(submit({ agent_id: 'ws', text: 'question', scope })).rejects.toThrow()
    expect((await submit({ agent_id: 'ws', text: 'question', scope })).run_id).toBe('run-1')
    const requests = fetcher.mock.calls.map(call => call[1]!)
    expect(requests.map(req => new Headers(req.headers).get('Idempotency-Key'))).toEqual(['stable-key', 'stable-key'])
    expect(JSON.parse(requests[1].body as string)).toEqual({ agent_id: 'ws', mode: 'qa', input: { text: 'question' }, business_context: { query_scope: scope } })
  })

  it('submits pure LLM QA with an empty business context and reuses the retry key', async () => {
    const fetcher = vi.fn<typeof fetch>().mockRejectedValueOnce(new TypeError('network')).mockResolvedValueOnce(Response.json({ run_id: 'run-1' }, { status: 202 }))
    const submit = createQaSubmission(new ApiClient(() => 'token', fetcher), () => 'stable-key')
    const request = { agent_id: 'ws', text: 'question' }
    await expect(submit(request)).rejects.toThrow('network')
    expect((await submit(request)).run_id).toBe('run-1')
    expect(fetcher.mock.calls[1][0]).toBe('/v1/runs')
    const init = fetcher.mock.calls[1][1]!
    expect(init.method).toBe('POST')
    expect(new Headers(init.headers).get('Authorization')).toBe('Bearer token')
    expect(new Headers(init.headers).get('Idempotency-Key')).toBe('stable-key')
    expect(JSON.parse(init.body as string)).toEqual({ agent_id: 'ws', mode: 'qa', input: { text: 'question' }, business_context: {} })
  })

  it('uses a new idempotency key when switching from pure LLM to scoped QA', async () => {
    const fetcher = vi.fn<typeof fetch>().mockRejectedValueOnce(new TypeError('network')).mockResolvedValueOnce(Response.json({ run_id: 'run-2' }, { status: 202 }))
    const keys = ['first', 'second']
    const submit = createQaSubmission(new ApiClient(() => 'token', fetcher), () => keys.shift()!)
    await expect(submit({ agent_id: 'ws', text: 'question' })).rejects.toThrow()
    await submit({ agent_id: 'ws', text: 'question', scope })
    expect(new Headers(fetcher.mock.calls[1][1]?.headers).get('Idempotency-Key')).toBe('second')
  })

  it('returns server errors and fetches the persisted run by id', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValueOnce(Response.json({ error: { code: 'UNAUTHENTICATED' } }, { status: 401 }))
      .mockResolvedValueOnce(Response.json({ run_id: '123', status: 'succeeded', outcome: { summary: 'answer' } }))
    const client = new ApiClient(() => 'token', fetcher)
    await expect(client.run('123')).rejects.toBeInstanceOf(ApiError)
    expect((await client.run('123')).outcome?.summary).toBe('answer')
    expect(fetcher.mock.calls[1][0]).toBe('/v1/runs/123')
    expect(isRunActive('queued')).toBe(true)
    expect(isRunActive('running')).toBe(true)
    expect(isRunActive('succeeded')).toBe(false)
    expect(isRunActive('waiting_input')).toBe(true)
    expect(isRunActive('waiting_approval')).toBe(true)
  })

  it('uses a new idempotency key when the question changes after a failure', async () => {
    const fetcher = vi.fn<typeof fetch>().mockRejectedValueOnce(new TypeError('network')).mockResolvedValueOnce(Response.json({ run_id: 'run-2' }, { status: 202 }))
    const keys = ['first', 'second']
    const submit = createQaSubmission(new ApiClient(() => 'token', fetcher), () => keys.shift()!)
    await expect(submit({ agent_id: 'ws', text: 'first', scope })).rejects.toThrow()
    await submit({ agent_id: 'ws', text: 'second', scope })
    expect(new Headers(fetcher.mock.calls[1][1]?.headers).get('Idempotency-Key')).toBe('second')
  })

  it('accepts only a positive window of at most one hour', () => {
    expect(validQaWindow(new Date('2026-11-16T01:00:00Z'), new Date('2026-11-16T02:00:00Z'))).toBe(true)
    expect(validQaWindow(new Date('2026-11-16T01:00:00Z'), new Date('2026-11-16T02:01:00Z'))).toBe(false)
    expect(validQaWindow(new Date('2026-11-16T01:00:00Z'), new Date('2026-11-16T01:00:00Z'))).toBe(false)
    expect(validQaWindow(new Date('invalid'), new Date())).toBe(false)
  })
})

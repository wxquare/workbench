import { expect, it, vi } from 'vitest'
import { ApiClient } from '../src/api'
import { listThreads, listMessages, submitConversation } from '../src/threads-api'
import { listRuns } from '../src/runs-api'

it('keeps each agent associated with its own threads, submitted runs, and run history', async () => {
  const fetcher = vi.fn<typeof fetch>().mockImplementation(async (url, init) => {
    const path = new URL(String(url), 'https://test.local')
    if (init?.method === 'POST') {
      const body = JSON.parse(init.body as string)
      if (body.agent_id === 'dod-agent' && body.thread_id === 'dod-thread')
        return Response.json({ run_id: 'dod-run', thread_id: 'dod-thread' })
      if (body.agent_id === 'writing-agent' && body.thread_id === 'writing-thread')
        return Response.json({ run_id: 'writing-run', thread_id: 'writing-thread' })
      throw new Error('Incorrect agent/thread association')
    }
    const id = path.searchParams.get('agent_id')
    if (id !== 'dod-agent' && id !== 'writing-agent') throw new Error('Missing agent scope')
    const prefix = id === 'dod-agent' ? 'dod' : 'writing'
    if (path.pathname === '/v1/threads') return Response.json({ items: [{ agent_id: id, thread_id: `${prefix}-thread`,
      title: prefix, status: 'succeeded', last_run_id: `${prefix}-run`, created_at: '2026-11-17T00:00:00Z', updated_at: '2026-11-17T00:00:00Z' }], next_cursor: null })
    if (path.pathname === '/v1/runs') return Response.json({ items: [{ agent_id: id, thread_id: `${prefix}-thread`,
      run_id: `${prefix}-run`, title: prefix, mode: 'qa', status: 'succeeded', phase: null, created_at: '2026-11-17T00:00:00Z' }], next_cursor: null })
    throw new Error(`Unexpected path: ${path}`)
  })
  const client = new ApiClient(() => 'token', fetcher)
  const dodThreads = await listThreads(client, 'dod-agent')
  const writingThreads = await listThreads(client, 'writing-agent')
  expect(dodThreads.items[0].agent_id).toBe('dod-agent')
  expect(writingThreads.items[0].agent_id).toBe('writing-agent')
  expect(await submitConversation(client, 'dod-agent', dodThreads.items[0].thread_id, 'Investigate', 'dod-key'))
    .toEqual({ run_id: 'dod-run', thread_id: 'dod-thread' })
  expect(await submitConversation(client, 'writing-agent', writingThreads.items[0].thread_id, 'Edit', 'writing-key'))
    .toEqual({ run_id: 'writing-run', thread_id: 'writing-thread' })
  expect((await listRuns(client, 'dod-agent')).items[0]).toMatchObject({ agent_id: 'dod-agent', run_id: 'dod-run', thread_id: 'dod-thread' })
  expect((await listRuns(client, 'writing-agent')).items[0]).toMatchObject({ agent_id: 'writing-agent', run_id: 'writing-run', thread_id: 'writing-thread' })
})

it('loads agent conversations and their persisted messages', async () => {
  const fetcher = vi.fn<typeof fetch>().mockImplementation(async () => Response.json({ items: [], next_cursor: null }))
  const client = new ApiClient(() => 'token', fetcher)
  await listThreads(client, 'agent', 'cursor')
  await listMessages(client, 'thread')
  expect(fetcher.mock.calls[0][0]).toBe('/v1/threads?agent_id=agent&limit=20&after=cursor')
  expect(fetcher.mock.calls[1][0]).toBe('/v1/threads/thread/messages?limit=100')
})

it('requests a real conversation workflow with a stable supplied idempotency key', async () => {
  const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json({ run_id: 'run', thread_id: 'thread' }))
  const client = new ApiClient(() => 'token', fetcher)
  await submitConversation(client, 'agent', 'thread', 'question', 'key')
  const init = fetcher.mock.calls[0][1]!
  expect(new Headers(init.headers).get('Idempotency-Key')).toBe('key')
  expect(JSON.parse(init.body as string)).toEqual({ agent_id: 'agent', thread_id: 'thread', mode: 'qa', input: { text: 'question' }, business_context: { conversation: true } })
})

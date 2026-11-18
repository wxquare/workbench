import { describe, expect, it } from 'vitest'
import { parseEventStream, EventCursorExpired, openRunStream } from '../src/sse'

function response(chunks: string[], status = 200): Response {
  const encoder = new TextEncoder()
  return new Response(new ReadableStream({ start(controller) {
    for (const chunk of chunks) controller.enqueue(encoder.encode(chunk))
    controller.close()
  }}), { status, headers: { 'Content-Type': 'text/event-stream' } })
}

describe('SSE transport', () => {
  it('parses split frames, comments, and CRLF line endings', async () => {
    const events = []
    for await (const item of parseEventStream(response([': heartbeat\r\nid: r:1\r\nevent: run.', 'started\r\ndata: {"seq":1}\r\n\r\n']).body!)) events.push(item)
    expect(events).toEqual([{ id: 'r:1', event: 'run.started', data: '{"seq":1}' }])
  })
  it('preserves multiline data and ignores incomplete frames', async () => {
    const events = []
    for await (const item of parseEventStream(response(['data: first\ndata: second\n\ndata: incomplete']).body!)) events.push(item)
    expect(events).toEqual([{ id: '', event: 'message', data: 'first\nsecond' }])
  })
  it('reconnects with last event id and same-origin credentials', async () => {
    const seen: RequestInit[] = []
    const responses = [response(['id: r:1\ndata: {"seq":1}\n\n']), response(['id: r:2\ndata: {"seq":2}\n\n'])]
    const abort = new AbortController()
    const fetcher: typeof fetch = async (_url, options) => { seen.push(options!); return responses.shift()! }
    const events = []
    for await (const item of openRunStream('/v1/runs/r/events', { fetcher, signal: abort.signal, retryMs: 0 })) {
      events.push(item.id)
      if (events.length === 2) abort.abort()
    }
    expect(events).toEqual(['r:1', 'r:2'])
    expect(seen[1].credentials).toBe('same-origin')
    expect(new Headers(seen[1].headers).get('Last-Event-ID')).toBe('r:1')
  })
  it('reports expired cursors without retrying', async () => {
    const fetcher: typeof fetch = async () => Response.json({ links: { result: '/v1/runs/r' } }, { status: 410 })
    const iterator = openRunStream('/v1/runs/r/events', { fetcher, retryMs: 0 })
    await expect(iterator.next()).rejects.toMatchObject({ resultLink: '/v1/runs/r' })
    expect(EventCursorExpired).toBeDefined()
  })
})

it('stops after the terminal event stream is drained', async () => {
  let calls = 0
  const fetcher: typeof fetch = async () => { calls += 1; return response(['id: r:1\nevent: run.completed\ndata: {}\n\n']) }
  const events = []
  for await (const item of openRunStream('/v1/runs/r/events', { fetcher, retryMs: 0 })) events.push(item)
  expect(events).toHaveLength(1)
  expect(calls).toBe(1)
})

it('reports repeated transport failures so the caller can fall back to polling', async () => {
  let attempts = 0
  const fetcher: typeof fetch = async () => { attempts++; throw new TypeError('offline') }
  const iterator = openRunStream('/v1/runs/r/events', { fetcher, retryMs: 0, maxFailures: 3 })
  await expect(iterator.next()).rejects.toThrow('SSE transport unavailable')
  expect(attempts).toBe(3)
})

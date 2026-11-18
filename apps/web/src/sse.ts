/** Same-origin SSE transport; committed event IDs support durable replay. */
export interface SseMessage { id: string; event: string; data: string }
export class EventCursorExpired extends Error {
  constructor(public readonly resultLink: string | null = null) { super('SSE cursor expired') }
}
export class StreamHttpError extends Error {
  constructor(public readonly status: number) { super(`SSE HTTP ${status}`) }
}

export async function* parseEventStream(stream: ReadableStream<Uint8Array>): AsyncGenerator<SseMessage> {
  const reader = stream.getReader()
  const decoder = new TextDecoder()
  let buffer = '', id = '', event = 'message', data: string[] = []
  try {
    while (true) {
      const part = await reader.read()
      buffer += decoder.decode(part.value, { stream: !part.done })
      if (buffer.length > 2_000_000) throw new Error('SSE frame too large')
      let position = 0
      while (position < buffer.length) {
        const end = buffer.slice(position).search(/[\r\n]/)
        if (end < 0) break
        const index = position + end
        if (buffer[index] === '\r' && index + 1 === buffer.length && !part.done) break
        const line = buffer.slice(position, index)
        position = index + (buffer[index] === '\r' && buffer[index + 1] === '\n' ? 2 : 1)
        if (!line) {
          if (data.length) yield { id, event, data: data.join('\n') }
          event = 'message'; data = []
        } else if (!line.startsWith(':')) {
          const separator = line.indexOf(':')
          const field = separator < 0 ? line : line.slice(0, separator)
          let value = separator < 0 ? '' : line.slice(separator + 1)
          if (value.startsWith(' ')) value = value.slice(1)
          if (field === 'data') data.push(value)
          else if (field === 'event') event = value || 'message'
          else if (field === 'id' && !value.includes('\0')) id = value
        }
      }
      buffer = buffer.slice(position)
      if (part.done) return
    }
  } finally {
    await reader.cancel().catch(() => undefined)
    reader.releaseLock()
  }
}

interface StreamOptions { signal?: AbortSignal; fetcher?: typeof fetch; retryMs?: number; maxFailures?: number; headers?: HeadersInit }
const terminal = new Set(['run.completed', 'run.failed', 'run.cancelled', 'run.expired'])
function wait(ms: number, signal?: AbortSignal): Promise<void> {
  if (signal?.aborted) return Promise.resolve()
  return new Promise(resolve => {
    const done = () => { clearTimeout(timer); signal?.removeEventListener('abort', done); resolve() }
    const timer = setTimeout(done, ms)
    signal?.addEventListener('abort', done, { once: true })
  })
}
export async function* openRunStream(path: string, options: StreamOptions = {}): AsyncGenerator<SseMessage> {
  if (!path.startsWith('/v1/runs/') || path.startsWith('//')) throw new Error('SSE URL must be a same-origin Run path')
  const fetcher = options.fetcher ?? fetch
  const retryMs = options.retryMs ?? 1000
  let cursor = '', terminalSeen = false, failures = 0
  const failed = () => { if (++failures >= (options.maxFailures ?? Infinity)) throw new Error('SSE transport unavailable') }
  while (!options.signal?.aborted) {
    const headers = new Headers(options.headers)
    headers.set('Accept', 'text/event-stream')
    if (cursor) headers.set('Last-Event-ID', cursor)
    let response: Response
    try {
      response = await fetcher(path, { headers, credentials: 'same-origin', signal: options.signal })
    } catch (error) {
      if (options.signal?.aborted) return
      failed()
      await wait(retryMs, options.signal)
      continue
    }
    if (response.status === 410) {
      let resultLink: string | null = null
      try {
        const body: unknown = await response.json()
        if (typeof body === 'object' && body !== null && 'links' in body) {
          const links = (body as { links?: { result?: unknown } }).links
          if (typeof links?.result === 'string' && /^\/v1\/runs\/[a-zA-Z0-9-]+$/.test(links.result)) resultLink = links.result
        }
      } catch { /* Expired cursor remains explicit even without a valid response body. */ }
      throw new EventCursorExpired(resultLink)
    }
    if (response.status >= 400 && response.status < 500) throw new StreamHttpError(response.status)
    if (!response.ok || !response.body) { failed(); await wait(retryMs, options.signal); continue }
    try {
      for await (const item of parseEventStream(response.body)) {
        if (options.signal?.aborted) return
        failures = 0
        cursor = item.id
        if (terminal.has(item.event)) terminalSeen = true
        yield item
      }
    } catch (error) {
      if (options.signal?.aborted) return
      // A transport break resumes from the last fully parsed event.
    }
    if (terminalSeen || options.signal?.aborted) return
    failed()
    await wait(retryMs, options.signal)
  }
}

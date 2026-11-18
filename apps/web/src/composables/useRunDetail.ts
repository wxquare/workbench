import { computed, onScopeDispose, ref, watch, type Ref } from 'vue'
import { ApiError, isRunActive, type RunView } from '../api'
import { applyEvent, initialRunState, type RunEvent } from '../run-stream'
import { shouldRefresh, parseWaiting } from '../run-detail-state'
import { EventCursorExpired, StreamHttpError, type SseMessage } from '../sse'

type Stream = (path: string, options: { signal: AbortSignal; headers: HeadersInit; maxFailures: number }) => AsyncGenerator<SseMessage>
interface Dependencies { getRun: (id: string) => Promise<RunView>; stream: Stream }
export function useRunDetail(id: Ref<string>, token: Ref<string>, deps: Dependencies) {
  const run = ref<RunView | null>(null), error = ref(''), loading = ref(false)
  const connection = ref<'idle' | 'live' | 'polling' | 'closed'>('idle')
  const state = ref(initialRunState(id.value))
  const waiting = computed(() => parseWaiting(run.value?.waiting ?? null))
  const draft = computed(() => Object.values(state.value.messages).filter(m => !m.superseded).map(m => m.text).join(''))
  let generation = 0, request = 0, poll: ReturnType<typeof setTimeout> | undefined, debounce: ReturnType<typeof setTimeout> | undefined
  let controller: AbortController | undefined
  const clear = () => { controller?.abort(); clearTimeout(poll); clearTimeout(debounce) }
  async function refresh(current = generation): Promise<RunView | null> {
    if (!token.value.trim()) { error.value = '请先提供 Bearer Token。'; return null }
    const sequence = ++request
    loading.value = true
    try {
      const value = await deps.getRun(id.value)
      if (current !== generation || sequence !== request) return null
      run.value = value; error.value = ''
      if (!isRunActive(value.status)) { clear(); connection.value = 'closed' }
      return value
    } catch (e) {
      if (current !== generation || sequence !== request) return null
      error.value = e instanceof Error ? e.message : '加载任务失败'
      if (e instanceof ApiError && [401, 403, 404].includes(e.status)) { run.value = null; clear(); connection.value = 'closed' }
      return null
    } finally { if (current === generation && sequence === request) loading.value = false }
  }
  function schedulePolling(current: number, count = 0) {
    if (current !== generation) return
    connection.value = 'polling'
    poll = setTimeout(async () => {
      if (current !== generation) return
      const value = await refresh(current)
      if (current !== generation || connection.value === 'closed') return
      if (value && !isRunActive(value.status)) return
      if (count >= 4) void connect(current)
      else schedulePolling(current, count + 1)
    }, 3000)
  }
  async function connect(current: number) {
    if (current !== generation) return
    controller?.abort(); controller = new AbortController()
    const active = controller
    connection.value = 'live'
    try {
      for await (const item of deps.stream(`/v1/runs/${encodeURIComponent(id.value)}/events`, { signal: active.signal, headers: { Authorization: `Bearer ${token.value.trim()}` }, maxFailures: 3 })) {
        if (current !== generation || active.signal.aborted) return
        let event: RunEvent
        try { event = JSON.parse(item.data) as RunEvent } catch { continue }
        state.value = applyEvent(state.value, event)
        if (shouldRefresh(event.type)) { clearTimeout(debounce); debounce = setTimeout(() => void refresh(current), 250) }
      }
      if (current !== generation || active.signal.aborted) return
      const value = await refresh(current)
      if (current === generation && value && isRunActive(value.status)) schedulePolling(current)
    } catch (e) {
      if (current !== generation || active.signal.aborted) return
      if (e instanceof EventCursorExpired) { await refresh(current); if (current === generation) connection.value = 'closed' }
      else if (e instanceof StreamHttpError && [401, 403, 404].includes(e.status)) { error.value = '无权限访问任务或任务不存在。'; connection.value = 'closed' }
      else schedulePolling(current)
    }
  }
  function restart() {
    generation++; request++; clear(); run.value = null; error.value = ''; state.value = initialRunState(id.value); connection.value = 'idle'
    const current = generation
    void refresh(current).then(value => { if (current === generation && value && isRunActive(value.status)) void connect(current) })
  }
  watch([id, token], restart, { immediate: true })
  onScopeDispose(() => { generation++; clear() })
  function applyRunView(value: RunView) { run.value = value; if (!isRunActive(value.status)) { clear(); connection.value = 'closed' } }
  return { run, error, loading, connection, waiting, draft, refresh: () => refresh(), applyRunView }
}

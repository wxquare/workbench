import { effectScope, nextTick, ref } from 'vue'
import { describe, expect, it, vi } from 'vitest'
import { useRunDetail } from '../src/composables/useRunDetail'
import type { RunView } from '../src/api'
const view = (id: string, status: string): RunView => ({ run_id: id, agent_id: 'a', thread_id: 't', status, phase: null, created_at: '2026-01-01T00:00:00Z', revision: 1, waiting: null, outcome: null })
const flush = async () => { await Promise.resolve(); await Promise.resolve(); await nextTick(); await Promise.resolve() }
describe('run detail lifecycle', () => {
  it('does not connect a terminal run', async () => {
    const scope = effectScope(), stream = vi.fn(async function* () {})
    const result = scope.run(() => useRunDetail(ref('r'), ref('token'), { getRun: async () => view('r', 'succeeded'), stream }))!
    await flush()
    expect(result.run.value?.status).toBe('succeeded')
    expect(stream).not.toHaveBeenCalled()
    scope.stop()
  })
  it('ignores a delayed result after switching runs', async () => {
    let resolve!: (v: RunView) => void
    const id = ref('old'), scope = effectScope()
    const result = scope.run(() => useRunDetail(id, ref('token'), { getRun: (key) => key === 'old' ? new Promise(r => { resolve = r }) : Promise.resolve(view('new', 'succeeded')), stream: async function* () {} }))!
    id.value = 'new'; await flush(); resolve(view('old', 'succeeded')); await flush()
    expect(result.run.value?.run_id).toBe('new')
    scope.stop()
  })
  it('falls back to polling after stream failures', async () => {
    const scope = effectScope()
    const result = scope.run(() => useRunDetail(ref('r'), ref('token'), { getRun: async () => view('r', 'running'), stream: async function* () { throw new Error('offline') } }))!
    await flush(); await flush()
    expect(result.connection.value).toBe('polling')
    scope.stop()
  })
})

it('retries the initial Run request after a transient failure', async () => {
  vi.useFakeTimers()
  const scope = effectScope(), getRun = vi.fn().mockRejectedValueOnce(new Error('offline')).mockResolvedValue(view('r', 'succeeded'))
  try {
    const result = scope.run(() => useRunDetail(ref('r'), ref('token'), { getRun, stream: async function* () {} }))!
    await flush(); expect(result.connection.value).toBe('polling')
    await vi.advanceTimersByTimeAsync(3000); await flush()
    expect(result.run.value?.status).toBe('succeeded')
  } finally { scope.stop(); vi.useRealTimers() }
})

it('does not let an earlier GET overwrite an accepted cancellation', async () => {
  const scope = effectScope()
  let resolve!: (value: RunView) => void
  const getRun = vi.fn().mockResolvedValueOnce(view('r', 'running')).mockImplementationOnce(() => new Promise(r => { resolve = r }))
  const result = scope.run(() => useRunDetail(ref('r'), ref('token'), { getRun, stream: async function* () { await new Promise(() => {}) } }))!
  await flush()
  const stale = result.refresh()
  result.applyRunView(view('r', 'cancelling'))
  resolve(view('r', 'running')); await stale; await flush()
  expect(result.run.value?.status).toBe('cancelling')
  scope.stop()
})

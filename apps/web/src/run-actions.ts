import { ref } from 'vue'
import { ApiError, type ResumeBody, type RunView } from './api'
import { createIntentKeys } from './run-detail-state'
interface Dependencies {
  cancel: (id: string, key: string) => Promise<RunView>
  resume: (id: string, body: ResumeBody, key: string) => Promise<unknown>
  refresh: () => unknown
  apply: (run: RunView) => void
  current: () => RunView | null
  makeKey?: () => string
}
export function createRunActions(deps: Dependencies) {
  const busy = ref(false), pending = ref(false), error = ref(''), readOnly = ref(false)
  const keys = createIntentKeys(deps.makeKey)
  let generation = 0
  function reset(preserveError = false) { generation++; busy.value = false; pending.value = false; if (!preserveError) error.value = ''; readOnly.value = false; keys.clear() }
  async function act(kind: 'cancel' | 'resume', body?: ResumeBody) {
    const run = deps.current()
    if (!run || busy.value || pending.value) return
    const current = generation, id = run.run_id
    const version = body ? `${body.interrupt_id}:${body.expected_revision}` : run.status
    const key = keys.key(kind, version, body ?? null)
    busy.value = true; error.value = ''
    try {
      if (kind === 'cancel') {
        const result = await deps.cancel(id, key)
        if (current !== generation || deps.current()?.run_id !== id) return
        deps.apply(result)
      } else {
        await deps.resume(id, body!, key)
        if (current !== generation || deps.current()?.run_id !== id) return
        pending.value = true
        void deps.refresh()
      }
      keys.clear()
    } catch (e) {
      if (current !== generation || deps.current()?.run_id !== id) return
      const code = e instanceof ApiError ? e.code : undefined
      if (['REVISION_CONFLICT', 'INTERRUPT_CONFLICT', 'APPROVAL_CONFLICT', 'APPROVAL_INVALIDATED', 'STALE_QUERY_SCOPE'].includes(code ?? '')) { error.value = '状态已变化，请确认最新状态。'; readOnly.value = true; keys.clear(); void deps.refresh() }
      else if (['INTERRUPT_EXPIRED', 'APPROVAL_EXPIRED'].includes(code ?? '')) { error.value = '等待已过期。'; readOnly.value = true; keys.clear(); void deps.refresh() }
      else if (code === 'FINALIZATION_STARTED') { error.value = '任务已进入结果整理，无法取消。'; void deps.refresh() }
      else if (code === 'INTERRUPT_ACTOR_MISMATCH') { error.value = '只有发起人可以处理该等待。'; readOnly.value = true }
      else if (code === 'INVALID_RESPONSE' || (e instanceof ApiError && e.status === 422)) error.value = '输入不合法，请检查后重试。'
      else if (code === 'IDEMPOTENCY_CONFLICT') { error.value = '请求冲突，请刷新页面重试。'; keys.clear() }
      else if (e instanceof ApiError && (e.status === 403 || e.status === 404)) error.value = '无操作权限，或任务不存在。'
      else error.value = '请求未完成，请检查网络后重试。'
    } finally { if (current === generation) busy.value = false }
  }
  return { busy, pending, error, readOnly, cancel: () => act('cancel'), resume: (body: ResumeBody) => act('resume', body), reset }
}

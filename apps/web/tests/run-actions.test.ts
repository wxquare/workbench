import { describe, expect, it, vi } from 'vitest'
import { createRunActions } from '../src/run-actions'
import { ApiError, type ResumeBody, type RunView } from '../src/api'
const run = { run_id: 'r', status: 'waiting_input', waiting: { interrupt_id: 'i', revision: 1 } } as RunView
const body: ResumeBody = { interrupt_id: 'i', expected_revision: 1, response: { kind: 'user_input', values: { region: 'sg' } } }
describe('run actions', () => {
  it('reuses a key after uncertain failure and changes it for changed input', async () => {
    let n = 0
    const resume = vi.fn().mockRejectedValueOnce(new TypeError('offline')).mockResolvedValue({})
    const actions = createRunActions({ cancel: vi.fn(), resume, refresh: vi.fn(), apply: vi.fn(), current: () => run, makeKey: () => String(++n) })
    await actions.resume(body)
    await actions.resume(body)
    expect(resume.mock.calls.map(call => call[2])).toEqual(['1', '1'])
    actions.reset()
    await actions.resume({ ...body, response: { kind: 'user_input', values: { region: 'id' } } })
    expect(resume.mock.calls[2][2]).toBe('2')
  })
  it('refreshes conflicts and rejects duplicate clicks while pending', async () => {
    let reject!: (reason: unknown) => void
    const refresh = vi.fn(), resume = vi.fn(() => new Promise((_resolve, fail) => { reject = fail }))
    const actions = createRunActions({ cancel: vi.fn(), resume, refresh, apply: vi.fn(), current: () => run, makeKey: () => 'key' })
    const pending = actions.resume(body)
    await actions.resume(body)
    expect(resume).toHaveBeenCalledTimes(1)
    reject(new ApiError(409, 'conflict', 'REVISION_CONFLICT'))
    await pending
    expect(refresh).toHaveBeenCalledOnce()
    expect(actions.error.value).toContain('状态已变化')
  })
})

it('keeps accepted resumes disabled until the waiting state changes', async () => {
  const resume = vi.fn().mockResolvedValue({})
  const actions = createRunActions({ cancel: vi.fn(), resume, refresh: vi.fn(), apply: vi.fn(), current: () => run, makeKey: () => 'key' })
  await actions.resume(body)
  await actions.resume(body)
  expect(resume).toHaveBeenCalledTimes(1)
  expect(actions.pending.value).toBe(true)
  actions.reset()
  expect(actions.pending.value).toBe(false)
})

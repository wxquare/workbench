import { describe, expect, it } from 'vitest'
import { demoRuns, findRun, statusPresentation } from '../src/demo'

describe('preview data', () => {
  it('keeps analysis and external delivery status distinct', () => {
    const run = demoRuns.find(item => item.delivery === 'unknown')
    expect(run?.status).toBe('completed')
    expect(statusPresentation[run!.status].label).toBe('分析完成')
    expect(statusPresentation[run!.delivery!].label).toBe('结果待核对')
  })
  it('looks up an existing demo run and rejects unknown IDs', () => {
    expect(findRun('run-2026-1108')?.title).toContain('支付')
    expect(findRun('missing')).toBeUndefined()
  })
})

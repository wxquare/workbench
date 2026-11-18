import { describe, expect, it } from 'vitest'
import { parseWaiting, buildInputResume, buildApprovalResume, createIntentKeys } from '../src/run-detail-state'
const base = { name: 'foundation.input', kind: 'user_input', actor: 'alice', interrupt_id: 'i', revision: 4, expires_at_ms: 5000, created_at_ms: 0, schema: { required: ['region'], additionalProperties: false, properties: { region: { type: 'string' } }, type: 'object' } }
describe('run waiting and action intent', () => {
  it('accepts the exact supported schema independent of key order', () => {
    const waiting = parseWaiting(base, 9)
    expect(waiting?.kind).toBe('input')
    if (waiting?.kind !== 'input') throw new Error('missing input')
    expect(buildInputResume(waiting, ' sg ')).toEqual({ interrupt_id: 'i', expected_revision: 9, response: { kind: 'user_input', values: { region: 'sg' } } })
    expect(parseWaiting({ ...base, schema: { ...base.schema, additionalProperties: true } })?.kind).toBe('unsupported')
  })
  it('requires a valid approval hash and uses the waiting revision', () => {
    const hash = 'a'.repeat(64)
    const wait = { ...base, kind: 'approval', name: 'foundation.approval', schema: { type: 'approval-v1' }, approval_id: 'a', args_hash: hash }
    const parsed = parseWaiting(wait, 9)
    expect(parsed?.kind).toBe('approval')
    if (parsed?.kind !== 'approval') throw new Error('missing approval')
    expect(buildApprovalResume(parsed, 'reject').expected_revision).toBe(9)
    expect(buildApprovalResume(parsed, 'reject').response).toEqual({ kind: 'approval', approval_id: 'a', decision: 'reject', expected_args_hash: hash })
    expect(parseWaiting({ ...wait, args_hash: 'wrong' })?.kind).toBe('unsupported')
  })
  it('reuses a key only while the complete operation intent is unchanged', () => {
    let next = 0
    const intents = createIntentKeys(() => String(++next))
    expect(intents.key('resume', 'i:4', { region: 'sg' })).toBe('1')
    expect(intents.key('resume', 'i:4', { region: 'sg' })).toBe('1')
    expect(intents.key('resume', 'i:4', { region: 'id' })).toBe('2')
    expect(intents.key('resume', 'i:5', { region: 'id' })).toBe('3')
    intents.clear()
    expect(intents.key('resume', 'i:5', { region: 'id' })).toBe('4')
  })
})

it('refreshes the authoritative Run on start and terminal notifications', async () => {
  const { shouldRefresh } = await import('../src/run-detail-state')
  expect(shouldRefresh('run.started')).toBe(true)
  expect(shouldRefresh('run.completed')).toBe(true)
  expect(shouldRefresh('assistant.delta')).toBe(false)
})

import { describe, expect, it } from 'vitest'
import { initialRunState, applyEvent, type RunEvent } from '../src/run-stream'

const event = (seq: number, type: string, payload: Record<string, unknown>, attempt = 1, message_id: string | null = null): RunEvent => ({
  run_id: 'run-1', seq, type, payload, attempt, message_id,
})

describe('run stream reducer', () => {
  it('deduplicates replay and marks previous attempts as superseded', () => {
    let state = initialRunState('run-1')
    state = applyEvent(state, event(1, 'assistant.delta', { text: 'first' }, 1, 'draft-1'))
    state = applyEvent(state, event(1, 'assistant.delta', { text: 'first' }, 1, 'draft-1'))
    state = applyEvent(state, event(2, 'assistant.delta', { text: 'second' }, 2, 'draft-2'))
    expect(state.messages['draft-1']).toMatchObject({ text: 'first', superseded: true })
    expect(state.messages['draft-2']).toMatchObject({ text: 'second', superseded: false })
  })
  it('keeps terminal analysis and external action state independent', () => {
    let state = initialRunState('run-1')
    state = applyEvent(state, event(1, 'run.completed', { terminal_status: 'succeeded' }))
    state = applyEvent(state, event(2, 'action.updated', { action_id: 'a1', state: 'unknown' }))
    expect(state.analysisStatus).toBe('succeeded')
    expect(state.actions.a1.state).toBe('unknown')
    expect(state.lastSeq).toBe(2)
  })
  it('does not accept events from a different run', () => {
    const state = initialRunState('run-1')
    expect(applyEvent(state, { ...event(1, 'run.completed', { terminal_status: 'succeeded' }), run_id: 'run-2' })).toEqual(state)
  })
})

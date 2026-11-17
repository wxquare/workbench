/** Merge committed Run events without conflating analysis and delivery. */
export interface RunEvent {
  run_id: string
  seq: number
  type: string
  attempt: number
  message_id: string | null
  payload: Record<string, unknown>
}
export interface DraftMessage { text: string; attempt: number; superseded: boolean }
export interface UiRunState {
  runId: string
  lastSeq: number
  analysisStatus: string | null
  messages: Record<string, DraftMessage>
  actions: Record<string, Record<string, unknown>>
}
export const initialRunState = (runId: string): UiRunState => ({
  runId, lastSeq: 0, analysisStatus: null, messages: {}, actions: {},
})
export function applyEvent(state: UiRunState, event: RunEvent): UiRunState {
  if (event.run_id !== state.runId || !Number.isSafeInteger(event.seq) || event.seq <= state.lastSeq) return state
  const next: UiRunState = { ...state, lastSeq: event.seq }
  if (event.type === 'run.completed' || event.type === 'run.failed') {
    if (typeof event.payload.terminal_status === 'string') next.analysisStatus = event.payload.terminal_status
  } else if (event.type === 'run.cancelled') {
    next.analysisStatus = 'cancelled'
  } else if (event.type === 'run.expired') {
    next.analysisStatus = 'expired'
  } else if (event.type === 'action.updated' && typeof event.payload.action_id === 'string') {
    next.actions = { ...state.actions, [event.payload.action_id]: { ...event.payload } }
  } else if (event.type === 'assistant.delta' && event.message_id && typeof event.payload.text === 'string') {
    next.messages = Object.fromEntries(Object.entries(state.messages).map(([id, message]) => [id,
      message.attempt < event.attempt ? { ...message, superseded: true } : message]))
    const prior = next.messages[event.message_id]
    if (!prior || prior.attempt === event.attempt) {
      next.messages[event.message_id] = { text: (prior?.text ?? '') + event.payload.text,
        attempt: event.attempt, superseded: prior?.superseded ?? false }
    }
  } else if (event.type === 'assistant.message_superseded') {
    const id = event.message_id ?? event.payload.message_id
    if (typeof id === 'string' && state.messages[id]) {
      next.messages = { ...state.messages, [id]: { ...state.messages[id], superseded: true } }
    }
  }
  return next
}

import type { ResumeBody, WaitingView } from './api'
export type WaitingModel =
  | { kind: 'input'; interruptId: string; revision: number; expiresAt: number; scope?: string }
  | { kind: 'approval'; interruptId: string; revision: number; expiresAt: number; approvalId: string; argsHash: string; actor: string }
  | { kind: 'unsupported'; reason: string }
const sameKeys = (value: object, keys: string[]) => Object.keys(value).sort().join(',') === keys.sort().join(',')
export function parseWaiting(w: WaitingView | null): WaitingModel | null {
  if (!w) return null
  const schema = w.schema
  if (w.kind === 'approval' && schema && sameKeys(schema, ['type']) && schema.type === 'approval-v1' && w.approval_id && /^[0-9a-f]{64}$/.test(w.args_hash ?? ''))
    return { kind: 'approval', interruptId: w.interrupt_id, revision: w.revision, expiresAt: w.expires_at_ms, approvalId: w.approval_id, argsHash: w.args_hash!, actor: w.actor ?? '' }
  const properties = schema?.properties
  if (w.kind === 'user_input' && schema && sameKeys(schema, ['type', 'properties', 'required', 'additionalProperties']) && schema.type === 'object' && schema.additionalProperties === false && Array.isArray(schema.required) && schema.required.length === 1 && schema.required[0] === 'region' && properties && typeof properties === 'object' && sameKeys(properties, ['region'])) {
    const region = (properties as Record<string, unknown>).region
    if (region && typeof region === 'object' && sameKeys(region, ['type']) && (region as Record<string, unknown>).type === 'string')
      return { kind: 'input', interruptId: w.interrupt_id, revision: w.revision, expiresAt: w.expires_at_ms, scope: w.expected_scope_id }
  }
  return { kind: 'unsupported', reason: `无法在网页处理的等待类型：${w.name}` }
}
export function buildInputResume(w: Extract<WaitingModel, { kind: 'input' }>, region: string): ResumeBody {
  return { interrupt_id: w.interruptId, expected_revision: w.revision, response: { kind: 'user_input', values: { region: region.trim() } } }
}
export function buildApprovalResume(w: Extract<WaitingModel, { kind: 'approval' }>, decision: 'approve' | 'reject'): ResumeBody {
  return { interrupt_id: w.interruptId, expected_revision: w.revision, response: { kind: 'approval', approval_id: w.approvalId, decision, expected_args_hash: w.argsHash } }
}
export function createIntentKeys(makeKey: () => string = () => crypto.randomUUID()) {
  let previous = '', current = ''
  return { key(operation: string, version: string, body: unknown) { const signature = JSON.stringify([operation, version, body]); if (signature !== previous) { previous = signature; current = makeKey() } return current }, clear() { previous = ''; current = '' } }
}
export const REFRESH_EVENTS = new Set(['run.accepted', 'run.started', 'run.status_changed', 'phase.changed', 'input.required', 'approval.required', 'run.resumed', 'run.recovery_scheduled', 'run.completed', 'run.failed', 'run.cancelled', 'run.expired', 'action.updated'])
export const shouldRefresh = (type: string) => REFRESH_EVENTS.has(type)
export const waitingRemainingMs = (w: { expiresAt: number }, now = Date.now()) => Math.max(0, w.expiresAt - now)

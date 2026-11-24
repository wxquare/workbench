import type { ApiClient } from './api'

export interface ActionDecision {
  action_id: string
  approval_id: string
  state: string
  payload_hash: string
}

export function decideAction(
  client: ApiClient,
  approvalId: string,
  decision: 'approve' | 'reject',
  expectedArgsHash: string,
): Promise<ActionDecision> {
  return client.request(
    `/v1/approvals/${encodeURIComponent(approvalId)}/decision`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        decision,
        expected_args_hash: expectedArgsHash,
      }),
    },
  )
}

export function reconcileAction(
  client: ApiClient,
  actionId: string,
  key: string,
): Promise<{ action_id: string; state: string; receipt?: unknown }> {
  return client.request(
    `/v1/actions/${encodeURIComponent(actionId)}/reconcile`,
    {
      method: 'POST',
      headers: { 'Idempotency-Key': key },
    },
  )
}

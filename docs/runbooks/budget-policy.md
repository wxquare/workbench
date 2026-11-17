---
title: "Budget policy: admission, recovery and historical Runs"
---

## Creating a Run with a fixed summary hold

Run admission persists the budget policy in the same transaction as the Run, initial Message, Job, Event and idempotent response. The policy exists before the first Worker claim or reservation. Rejected reservations, crashes and lease takeovers cannot change it.

The default summary hold is one model call plus the `max_context_tokens` of the summary-role model version in the Run's frozen context. With the current 64,000-token model and the QA budget of 80,000 tokens, experts and evaluations share 16,000 available tokens before summary composition. The context cap and the parent token budget are checked independently.

The stable `POST /v1/runs` request accepts optional `summary_token_reserve`, a positive integer no larger than that frozen summary model's context limit. For example:

```json
{
  "agent_id": "00000000-0000-0000-0000-000000000001",
  "mode": "qa",
  "input": {"type": "text", "text": "Investigate the failure"},
  "budgets": {"tokens": 2000},
  "summary_token_reserve": 1000
}
```

Choose a smaller reserve only when the actual summary request's safe input-plus-output token bound fits it. A summary request exceeding the fixed hold is rejected; it cannot borrow other work's allowance. A total Run token budget below the hold is allowed at admission, but network work that cannot retain the hold must use the deterministic incomplete Outcome path. Missing provider usage or pricing remains pending.

`BudgetService(summary_tokens=...)` now only checks consistency with the admitted policy. A service with no explicit override reads the durable policy. An explicitly different value returns `BUDGET_POLICY_CONFLICT`; changing a process configuration never rewrites an existing Run's allowance. Create a new Run to intentionally choose a different policy.

## Historical Runs without a policy

Migration `0006_usage_ledger` preserves existing Runs, configuration, events and typed checkpoints. It does not invent summary holds for old Runs: neither their frozen model context nor a zero/cancelled usage row can establish which custom summary reserve was originally intended. Any historical usage reservation or record causes that migration to fail before DDL and requires reviewed offline reconciliation.

An upgraded Run with no policy, or an existing 0006 Run admitted before this admission fix and never successfully reserved, remains readable through Run queries, budget snapshots and deterministic partial composition. Its snapshot reports `summary_token_reserve: null`. New reservations fail closed with `BUDGET_POLICY_UNINITIALIZED` and a `create_run` link to `/v1/runs`. The exception note identifies this runbook and recovery choices. The service never repairs the gap by reading the current process configuration.

Preserve the Run and ledger for audit. Review any possible external work before taking recovery action. To proceed under a known policy, create a replacement Run with a new idempotency key and an explicit reserve or the versioned default. Omit `client_message_id` or use a new one as well; reusing a retained source identifier replays the old Run. Use the normal cancellation endpoint if the old Run must release an active Thread position; do not delete history or resend an uncertain invocation. If the original Run must continue, arrange an explicit reviewed offline reconciliation backed by authoritative policy and usage evidence. There is no automatic policy backfill or bypass command.

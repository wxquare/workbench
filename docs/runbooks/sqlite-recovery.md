---
title: "SQLite recovery: consistent backups and explicit restore review"
---

## Operating boundary

This runbook retains M1 recovery details as historical capability boundaries. The Agent domain refactor changes the active schema and contracts; implementation and verification boundaries are recorded in [specification 21.4](../specs/2026-11-12-workbench-python-spec.md#214-移除-workspace-的-agent-领域重构). M1 acceptance does not establish readiness of the new baseline.

M1 uses one host, a local persistent disk, one absolute database path, one Worker with two execution slots, WAL/FULL/FK, a 5-second storage deadline, 45-second leases, renewal every 10 seconds and recovery scanning every 5 seconds. Never place SQLite on NFS or start a second Worker deployment. Every process must load the same explicit configuration. Ordinary API/Worker/Scheduler startup performs no migration or seed operation. They enforce the same declared local persistent-root, production temporary-path and read/write-access policy as doctor, together with current schema/driver checks. API readiness and admission recheck this policy; a backend injected for a restored copy must match its configured database path.

The scheduler scans durable recovery Jobs and deadlines. M1 does not implement business cron triggers, Action dispatch, external receipts, Runner process groups, or model invocation. Missing pinned graph capabilities must be restored by deploying the same graph implementation; never rewrite `runs.versions`, change published versions in place, or replace a checkpoint with a current graph.

## Explicit initial startup

For the Agent schema, explicitly select a fresh absolute SQLite filename, such as the example below. Keep the existing database, WAL/SHM companions, backups and object storage intact. Migration, doctor and readiness must safely reject the old Workspace schema and explain how to choose a new path and run explicit initialization. They must never delete data, reset tables, stamp the old database as current, or silently choose another path. Rebuild Agent configuration and deployment credential bindings explicitly; old Runs are not automatically imported. Normal startup does not migrate, bootstrap or delete databases.

The current source baseline is `schema_agent_v1.py`, the separate `persistence/agent_migrations/` chain at `0001_agent_domain`, storage protocol `dod-storage-agent-v1`, and codec `dod-checkpoint-v1`. Legacy migrations and snapshots are historical only. New Agents use independent draft defaults and a team-shared published model, without inheriting another Agent's configuration or credential access. No live database was modified as part of this refactor.

From `apps/agent`, with the locked environment installed. Set `WORKBENCH_ROOT` to the absolute path of the workbench project:

```sh
export WORKBENCH_ROOT=/Users/xianguiwang/Projects/workbench
export WORKBENCH_ASSET_ROOT="$WORKBENCH_ROOT"
export WORKBENCH_FRONTEND_DIST_ROOT="$WORKBENCH_ROOT/apps/web/dist"
export WORKBENCH_PERSISTENT_ROOT=/absolute/local/persistent/dod
export WORKBENCH_DATABASE_URL=sqlite+aiosqlite:////absolute/local/persistent/dod/agents-20261117.sqlite3
uv run workbench db migrate --config "$WORKBENCH_ROOT/config/local/agent.yaml"
uv run workbench bootstrap --config "$WORKBENCH_ROOT/config/local/agent.yaml"
uv run workbench doctor --config "$WORKBENCH_ROOT/config/local/agent.yaml"
uv run workbench api --config "$WORKBENCH_ROOT/config/local/agent.yaml" --host 127.0.0.1 --port 8000
```

Run `uv run workbench worker --config "$WORKBENCH_ROOT/config/local/agent.yaml"` and `uv run workbench scheduler --config "$WORKBENCH_ROOT/config/local/agent.yaml"` in separate supervised processes using the same environment. Worker shutdown permits 30 seconds for in-flight work, then cancels and performs fenced bookkeeping; API gives active connections 15 seconds before cancellation. Allow a small supervisor margin for bounded storage/connection cleanup (compose uses 35/20 seconds). SIGKILL has no graceful cleanup: replacement waits for the durable lease to expire.

`$WORKBENCH_ROOT/deploy/compose.local.yml` is a supplied configuration, not a Docker verification result. It uses the existing image entrypoint and the same local volume for all three processes. Run explicit maintenance commands with `docker compose ... run --rm api db migrate --config /config/agent.yaml` and bootstrap before `up`. This host had no Docker executable, so build/startup in Docker remains unverified.

## Consistent backup

```sh
uv run workbench db backup --config "$WORKBENCH_ROOT/config/local/agent.yaml" --destination /absolute/backup/dod-20261112.sqlite3
```

This uses SQLite Online Backup API from a read-only source, including committed WAL data while the source remains online. Each public backup, restore or acknowledgment starts one absolute5-second maintenance deadline. All nested copying, SQL validation, Python codec/parent-chain checks, chunked hashing and filesystem phases consume that same budget; no phase resets it. Connection and statement busy timeouts use only the remaining budget. Blocking filesystem calls cannot be preempted, so expiry is checked immediately after they return and before publication, removing quarantine or reporting success. Required rollback/close/reinstating-quarantine cleanup may finish after expiry, but cannot report success or authorize new work. The output contains its SHA256 and checkpoint/pending/artifact/action counts. Integrity, FK, current migration revision, checkpoint codec/namespace/parent-chain/pending-index and artifact metadata/reference validation precede atomic publication. Existing destination main files and every `-wal`, `-shm`, `-journal` companion are refused, including symlinks and dangling symlinks. No collision is removed. Keep the destination offline throughout publication; choose another empty path rather than deleting recovery evidence. Files are never copied from the main DB alone or overwritten blindly. Backup/restore errors do not print row contents or secrets.

Retain object-store backups independently. Database Artifact metadata and references do not prove object existence, content or integrity. M1 has no configured real object signer by default and the authorized artifact URL endpoint returns 503 in that configuration.

## Restore to a new path

Stop any process intended to use the target. Keep the source and backup intact. Never overwrite an active database or delete its WAL/SHM files to make a restore appear clean.

Restore is for a compatible schema, storage protocol and checkpoint codec; it does not convert a legacy Workspace database into an Agent database. Use the matching historical tooling in an isolated environment to inspect legacy backups. Keep an incompatible backup and its original files intact, then initialize the Agent baseline at a separate explicitly selected path.

```sh
uv run workbench db restore --config "$WORKBENCH_ROOT/config/local/agent.yaml" \
  --source /absolute/backup/dod-20261112.sqlite3 \
  --target /absolute/local/persistent/dod/restored.sqlite3
```

Restore repeats integrity/FK/schema, typed checkpoint payload/metadata, namespace hash, complete parent chain (including cycles), pending-write reference/index, artifact metadata and evidence-reference checks. An incompatible codec, missing parent, orphan write or invalid Artifact reference refuses restore. It preserves original Run/Job/version/receipt/marker data. In particular, an `unknown` Action is never classified as completed or retried.

The target is published only with `restored.sqlite3.restore-quarantine.json` already persisted. It states `external_writes_enabled=false`, `dispatcher_enabled=false`, and that object/action reconciliation is required. Readiness, doctor and **every ordinary backend write UoW** reject the quarantined target; a writer rechecks quarantine after obtaining `BEGIN IMMEDIATE` and before any mutation, so waiting behind a failed acknowledgment cannot bypass a recreated marker; read-only SQLite inspection is possible. This prevents accidentally starting API/Worker/Scheduler before review. The marker and database must be retained together. Do not remove the marker manually to bypass the gate.

## Explicit clean-M1 review and reopening

Inspect the restored copy offline, retain the original backup, confirm the target deployment points at the intended absolute local path, and verify the checkpoint/Run state. The M1 acknowledgment command is intentionally restricted to databases containing **zero Artifact, zero Action and zero Outbox rows**. If any exist, it refuses reopening; follow an independently reviewed offline object/action reconciliation procedure when those integrations exist. M1 does not provide such a procedure or claim M3 delivery recovery.

For a clean M1 copy only, the operator can explicitly acknowledge the review:

```sh
uv run workbench db acknowledge-restore --config "$WORKBENCH_ROOT/config/local/agent.yaml" \
  --target /absolute/local/persistent/dod/restored.sqlite3 \
  --acknowledge-no-external-effects
export WORKBENCH_DATABASE_URL=sqlite+aiosqlite:////absolute/local/persistent/dod/restored.sqlite3
uv run workbench doctor --config "$WORKBENCH_ROOT/config/local/agent.yaml"
uv run workbench scheduler --config "$WORKBENCH_ROOT/config/local/agent.yaml" --once
uv run workbench worker --config "$WORKBENCH_ROOT/config/local/agent.yaml" --once
```

The command revalidates the copy, holds an exclusive SQLite write transaction during the maintenance gate, records `restore-review.json` with acknowledgment time and the DB-file hash at review, then removes quarantine. It does not rewrite any business/action state. If target publication fails, quarantine is retained. All acknowledgment work, fsync/deadline checks and failure-marker reinstatement execute while its SQLite write lock is held. A failure before the final checked logical commit recreates quarantine before releasing that lock, so waiting runtime writers cannot pass the post-BEGIN gate. Leave the quarantine and any review record intact for offline inspection. An incomplete existing review record requires reviewed offline cleanup before retrying acknowledgment; never remove quarantine to bypass that failure. `dispatcher_enabled=false` remains in the review record; there is no Dispatcher command in M1. Start the API/Worker/Scheduler normally only after doctor reports ready. The acknowledgment logical commit is the final successful fsync/deadline check inside the held write lock; driver close/implicit rollback afterward is cleanup only. Cleanup may finish beyond the work deadline and must not retroactively restore quarantine. If cleanup raises, the CLI exits1 with `acknowledgment_committed=true`, `cleanup_completed=false`, `external_writes_enabled=true` and `dispatcher_enabled=false`. This explicitly reports an acknowledged open gate with uncertain cleanup, not retained quarantine. Inspect the durable review and storage readiness; do not blindly retry acknowledgment or invent a rollback of the already committed decision. Regression coverage actually reopens an acknowledged restored DB, executes a saved queued Run with Worker, and obtains its succeeded Outcome through the API. A copy with an unknown Action fails acknowledgment and remains quarantined.

## Recovery decisions and diagnostics

An expired owner first receives a new fenced epoch for **projection only**, with no new actual execution attempt. Native LangGraph state inspection happens outside the write UoW; the protected decision rechecks checkpoint ID and ownership in the short transaction.

| Durable evidence | Recovery behavior |
| --- | --- |
| Run already terminal | Return terminal without writing or replaying |
| Valid saved Outcome/end | Same-UoW result/message/event/release; old draft superseded once |
| Named interrupt checkpoint without public wait | Project exactly one waiting Interrupt; do not regenerate the question |
| Submitted/applying selected request + committed `last_applied_resume_id` | Mark request applied and Interrupt consumed without a second Command |
| Known runnable foundation next nodes, or no checkpoint before safe foundation start | Close old Job, create one recover generation, explicitly clear projection intent; actual graph work occurs later outside the transaction |
| Incompatible/unknown state, finalizing without valid Outcome, or uncertain Action requiring replay | Terminal `STATE_RECONCILIATION_REQUIRED`; preserve evidence for offline investigation |

Saved valid Outcome/wait/applied markers are considered before retry exhaustion. Three actual segment executions are the hard limit; control takeover can increase epoch without a fourth execution. Missing capability does not confer ownership or mutate epoch. A live owner cannot be expired by an unowned scheduler scan. A dead `cancel_requested` owner is taken over and fenced before cancellation cleanup. No `STALE_EXECUTION` retry loop exists.

Migration `0005_resume_protocol` canonicalizes only known, consistent aliases: request `accepted` → `submitted`, Interrupt `applied` → `consumed` when its selected pointer references the matching applied request. Ambiguous rows refuse migration atomically and require offline review; do not manually stamp the head revision. New requests use submitted → applying before Command → applied after the synchronous marker, and interrupts use open → response_submitted → consumed. Historical migrations 0001–0004 and graph/schema snapshots remain unchanged. Executed v2 databases still require the separate [timing migration policy](run-timing-migration.md).

## Troubleshooting scope

A Redis connection failure does not stop admission, Job discovery or recovery; DB polling remains authoritative, with SSE fallback at most one second. Healthy Redis pub/sub, enterprise connectors, DeepSeek inference and object storage need separate integration evidence. Never infer their readiness from `doctor`: it checks local storage only. For reproducible host evidence and exact test coverage, see [M1 acceptance](../acceptance/M1.md).

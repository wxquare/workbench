---
title: "Run timing migration: exact upgrades and ambiguous legacy history"
---

## Maintenance policy

Run schema migration through the explicit maintenance command, with services stopped and a verified local SQLite backup made using the Online Backup API or VACUUM INTO. Ordinary API and Worker startup never migrate. Migrations run in one BEGIN IMMEDIATE transaction.

Migration `0003_run_timing` adds durable first-start and active-interval fields. An empty v2 database, or one containing only never-started Runs (`run_epoch=0`), can upgrade automatically: `started_at=NULL`, `active_elapsed_ms=0`, `active_since=NULL`. Subsequent claims and releases record exact timing.

Any executed v2 Run (`run_epoch>0`) makes the automatic upgrade fail **before DDL**. This applies to running, waiting, queued retry, and terminal Runs. v2 has no durable interval ledger, so mutable deadlines, creation/update timestamps, and retained Events cannot reconstruct first start or time spent executing across waiting and retry gaps. The migration does not estimate these values or reset consumed budget.

## Responding to a refused upgrade

Retain the backup and the unchanged v2 database. Arrange an explicit, reviewed offline reconciliation policy supported by authoritative timing evidence before attempting an upgrade with executed legacy history. Do not bypass the check by resetting epochs, filling guessed timestamps, or deleting Runs. The migration itself performs no data deletion, archival, or timing reconstruction; a failed maintenance transaction preserves the original revision, columns, and rows.

`0003` was repaired during its unreleased T07 review and has not been deployed outside task scratch tests. Historical `0001`, `0002`, schema_v1, and schema_v2 remain unchanged. This review correction does not authorize rewriting a released migration or an existing deployment's history.

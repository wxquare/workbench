---
title: Agent Domain Refactor Implementation Plan
---

## Goal

Make 我的工作台 a shared workbench for multiple independently configured Agents. DoD is one business scenario, not the platform's fixed identity. Remove the product/domain Workspace layer from code and current documentation together.

## Contract

- Use agents, agent_id, agent_scope, agent_profile, /v1/agents and /app/agents.
- Preserve Team/Agent authorization, resource scope, immutable published profiles and Run snapshots.
- Keep container /workspace paths as execution-directory implementation details, not a user-facing entity.
- Accept a breaking database baseline; never silently delete or reinterpret an existing populated database.
- No new fixed agent_type taxonomy or unrelated package/product rename.
- Do not commit, push, or stop the user's running processes as part of this request.

## Tasks

- [x] Add failing API/schema tests for Agent creation, independent configurations, Run/Thread ownership and legacy rejection.
- [x] Convert backend domain, application, API, resource scopes and deployment configuration to Agent terminology.
- [x] Introduce an explicit Agent schema boundary with safe handling of legacy databases; update fixtures and regression tests.
- [x] Convert frontend modules, routes, types and tests to Agent contracts.
- [x] Update current specification, setup documentation and runbooks without rewriting historical acceptance records.
- [x] Run backend regressions, frontend tests/typecheck/build, schema safety tests and documentation checks; report deployment requirements.

## Verification

Backend: uv run pytest excluding opt-in live tests, with focused Agent-domain and conversation checks first. Frontend: npm test, npm run typecheck, npm run build. Validate an explicitly selected new SQLite database, ensure repeated initialization is safe, and verify a legacy database is not mutated or accepted as ready. Verify two differently configured Agents have distinct published settings and isolated task/conversation results.

## Results

2026-11-17: backend full suite 1148 passed, 17 skipped. The subsequent safe CLI guidance change passed 53 focused storage/readiness/resume tests and 5 wheel/package tests. Frontend 65 tests passed; typecheck and production build passed. The specification's 59 tables and 575 fields match current metadata. An isolated browser journey created and published a non-DoD Agent and verified a completed simulated-model task and its list association. Temporary preview servers were stopped; the live database and user services were not switched. See specification section 21.4 for deployment and verification boundaries.

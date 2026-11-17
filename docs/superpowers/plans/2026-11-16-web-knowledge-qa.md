# Web Knowledge QA Implementation Plan

> **For agentic workers:** Coordinate front-end and back-end changes through the existing Run API, then verify their integration.

**Goal:** An authorized user selects a Workspace, submits a knowledge question, and sees the persisted Worker result.

**Architecture:** The web client authenticates with the existing Bearer boundary and submits a QA Run to FastAPI. The Run carries a query scope that is persisted atomically with admission; the Worker uses a published graph, model and scoped knowledge binding to produce a durable result.

**Tech Stack:** Vue 3, TypeScript, Vite, FastAPI, SQLite, LangGraph, MCP.

## Global Constraints

- Keep credentials out of the repository and preserve tenant, Workspace and tool authorization.
- Do not alter existing published profiles or represent simulated responses as live results.
- Preserve existing unrelated working-tree changes.

### Task 1: Admission and Worker

- [x] Test authorized QA admission with a complete service/environment/region/time scope, duplicate requests and invalid scopes.
- [x] Persist the validated scope with the Run and verify a configured Worker can process it and persist a truthful result.
- [x] Document the explicit model, Workspace and knowledge-source setup needed for real execution.

### Task 2: Web Workflow

- [x] Test authentication, Workspace loading, QA submission, errors and result retrieval against the existing API contract.
- [x] Add the usable submit and result views, including in-progress, terminal, failed and refresh states.
- [x] Remove or clearly identify demo-only content adjacent to the real workflow.

### Task 3: Integration and Verification

- [x] Run the focused backend and frontend tests, frontend typecheck and build.
- [x] Exercise the browser workflow and independent Worker with controlled test services; report whether real credentials and a real source are available for live verification.

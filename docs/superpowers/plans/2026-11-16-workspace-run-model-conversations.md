# Workspace, Runs, Models and Conversations Plan

**Goal:** Replace the selected placeholder pages with authorized, durable workflows, including true multi-turn LLM questions.

**Architecture:** Add tenant/workspace-scoped inventories using existing authorization and cursor conventions. Reuse versioned workspace and model APIs, retain draft concurrency checks, and introduce a new pinned graph version for conversational history. Frontend uses the same authenticated API and reports failures and empty states honestly.

**Tech Stack:** FastAPI, SQLite, LangGraph, Vue, TypeScript, pytest, Vitest.

## Constraints

- Keep existing graphs, published profiles, user data and secrets intact.
- Do not publish or modify a live Workspace without user approval.
- Restrict lists and history to currently authorized workspaces; validate cursors and avoid leaking other tenants.
- Verify backend, frontend, and browser workflows with isolated storage and a controlled model.

## Tasks

- [x] Implement authenticated run inventory with pagination and real task UI.
- [x] Implement Workspace inventory, creation and versioned draft publication UI.
- [x] Implement model-profile inventory and safe draft, publication and probe UI.
- [x] Implement conversation inventory, messages and new immutable multi-turn graph.
- [x] Integrate navigation and run backend/frontend and isolated end-to-end tests.

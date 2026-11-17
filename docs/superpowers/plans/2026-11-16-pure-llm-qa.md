# Pure LLM QA Implementation Plan

**Goal:** Submit a question from the web, run the configured LLM in a Worker, and display its persisted answer without a knowledge source.

**Architecture:** Add the immutable `dod.v17` QA graph. Existing admission, durable jobs, model credential binding, budgets and result projection remain in use; the answer is labeled as model-generated rather than evidence-backed. The web supports direct QA without a query scope while retaining the scoped knowledge option.

**Tech Stack:** FastAPI, SQLite, LangGraph, Vue, TypeScript.

## Constraints

- Preserve published `dod.v3` and `dod.v16` semantics and existing runs.
- Do not store or print credentials, bypass authorization, or use real model requests without explicit user action.
- Workspace publication is explicit; preserve its model and other draft fields.
- Test using a controlled model and isolated storage; distinguish this from live verification.

## Tasks

- [x] Add failing API/Worker tests for QA without scope, a real model invocation adapter, persistent answer, failure and recovery; verify red before implementation.
- [x] Register `dod.v17`, restrict it to QA, invoke the pinned model through the existing budget and credential boundaries, and persist honest outcome states; run focused regression tests.
- [x] Add web direct-QA mode without mandatory knowledge fields and retain scoped mode; test submission and display.
- [x] Document explicit Workspace publication and restart steps, verify an isolated browser/API/Worker journey, then run backend and frontend regressions.

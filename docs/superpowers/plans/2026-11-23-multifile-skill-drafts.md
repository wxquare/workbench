# Multifile Skill Drafts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Edit, upload, publish and inspect complete Skill drafts containing text, scripts and images, with backward-compatible requests and lossless database migration.

**Architecture:** One canonical files_json object per draft, entries encoded as {encoding, content}. Published resources keep existing content-addressed objects and manifests. Scripts are resources, not executable actions. Images use Base64 in draft JSON and raw bytes in published objects.

**Tech Stack:** Python 3.12, FastAPI/Pydantic, SQLAlchemy/Alembic/SQLite, Vue 3/TypeScript, pytest/Vitest.

## Global Constraints

- Specification: docs/superpowers/specs/2026-11-23-multifile-skill-drafts-design.md, including the user's confirmed image/script scope.
- No S3 integration, no automatic script execution, no changes to Agent pinning or historical Run identities.
- Draft limits: 64 files, path length 256, main body 32000 UTF-8 bytes, attachment text 256 KiB, image 2 MiB, decoded total 8 MiB; create/save canonical request maximum 12 MiB.
- Support PNG/JPEG/GIF/WebP image signatures; all other editable resources must be UTF-8. Reject NUL text, unsafe paths, file/directory prefix collisions and .claude resources.
- Preserve existing user's uncommitted documentation. Use branch codex/multifile-skill-drafts. Tests use temporary databases only.

## Task 1: Schema migration and resource codec

**Files:** schema_agent_v6.py; agent_migrations/versions/0006_multifile_skill_drafts.py; schema.py; sqlite_diagnostics.py; cli/db.py; agent_migrations/env.py; skill_management/drafts.py; skill_management/validator.py; migration and unit/integration tests.

**Interfaces:** drafts.normalize_files(files) -> canonical resource map; drafts.bundle_for_files(name, description, files) -> ValidatedBundle; drafts.encode_resources(resources) -> resource map. Resource maps use {"SKILL.md": {"encoding": "utf-8", "content": "body"}}. validate_bundle receives an explicit allow_scripts=False option; shared draft generation opts in, existing import restrictions remain.

- [x] Write migration test that creates a populated v5 database with a draft, published version and profile, migrates, and asserts JSON body and metadata preserved, constraints intact and health current.
- [x] Write tests for text/script/image roundtrip and rejection of unsafe paths, collisions, missing main, invalid Base64/image signature and oversized resources.
- [x] Run tests and observe missing feature failures.
- [x] Add v6 schema and migration: wrap legacy body into typed JSON, rename content to files_json without changing PK/FK/unique constraints; update current schema and preflight so populated v5 upgrades work.
- [x] Implement codec/limits and explicit script-resource validation; repeat targeted tests until green.

## Task 2: Shared Skill lifecycle and API

**Files:** skill_management/library.py; api/skill_library.py; application/idempotency.py; tests/integration/test_multifile_skills.py; shared-skill/version regressions.

**Interfaces:** create/save accept legacy content or new files; response draft always includes files and derived main content. History response includes all encoded files plus legacy complete main content. Legacy content-only save modifies main while retaining attachments. Simultaneous content/files must agree.

- [x] Write real HTTP/SQLite tests creating a main instruction, script and PNG, publishing, reading history and loading frozen resources through SkillLoader.
- [x] Add tests for old saves preserving attachments, conflicts, empty files, deletion/rename, idempotency retries, corrupt objects and v1/v2 isolation.
- [x] Observe expected red responses or assertions.
- [x] Replace draft content persistence with canonical files_json; retain request aliases; publish all decoded files and encode historical resources for API clients.
- [x] Raise create/save idempotency payload bounds only; keep publish operation's existing atomic DB/CAS behavior.
- [x] Run backend regressions and migration contracts; update tests referencing current migration head without altering old schema snapshots.

## Task 3: Multi-resource editor

**Files:** apps/web/src/skills-api.ts; views/Skills.vue; a focused SkillFilesEditor.vue component and resource utilities if needed; tests/skills-api.test.ts; tests/skills-view.test.ts; new editor tests.

**Interfaces:** SkillFile = {encoding: 'utf-8'|'base64'; content: string}; files is Record<string, SkillFile>. Draft response's content remains a compatibility projection; new editor sends files. History's SKILL.md includes generated metadata and is read-only. Backend accepts text strings as shorthand, but UI sends typed entries.

- [x] Write failing UI tests for adding/switching/renaming/deleting a script/reference, protected main file, image upload/preview, preserved edits, history attachments and read-only state.
- [x] Update API types and normalize old fixture/server drafts to typed maps.
- [x] Add accessible file selection/path controls and textarea, local text/image file upload with bounded FileReader handling, image preview, replacement and history views.
- [x] Keep unsaved-change and request generation guards, disable actions on loading/busy and reset state on authentication changes.
- [x] Run Vitest, typecheck and production build.

## Task 4: Documentation, end-to-end verification and review

**Files:** docs/02-architecture.md; docs/04-operations.md; docs/06-reference/01-api.md; docs/06-reference/02-data-dictionary.md; design/plan completion notes.

- [x] Document typed JSON, migration and legacy behavior, resource limits and script storage versus execution.
- [x] Execute backend offline suite and frontend suite/build/typecheck; inspect migration schema and valid published manifest file identities.
- [x] Run documentation checker; report pre-existing invalid verified_commit references separately from new failures.
- [x] Request independent review of implementation/spec compatibility; fix important findings with covering tests and reverify.
- [x] Record exact checks and remaining limits. Leave feature branch for review; do not merge, publish, or migrate the user's local business database.

## Completion

Implementation and independent review complete. Exact commands, environment retries and upgrade notes are recorded in `docs/superpowers/specs/2026-11-23-multifile-skill-drafts-verification.md`.

## Baseline

2026-11-23: 34 targeted backend tests and 3 frontend tests passed before implementation. Documentation checker already reports 11 invalid/non-ancestor verified_commit values and four over-800-line warnings.

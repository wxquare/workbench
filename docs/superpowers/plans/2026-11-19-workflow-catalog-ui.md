# Workflow Catalog UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rename the Graph Catalog UI to 工作流 and make the directory and B style graph detail easy to scan, including direct detail refresh.

**Architecture:** Keep the graph API and IDs. Restructure the Vue directory and detail templates, give `GraphView` a selectable node inspector, and add route specific SPA fallback for installed graph IDs. Add focused behavior tests before each change.

**Tech Stack:** Vue 3, TypeScript, Vitest, inline SVG, FastAPI, pytest.

## Global Constraints

- Eight registered graphs remain visible and bindable; `graph_id` and `/app/graphs` remain stable.
- Navigation and catalog copy say 工作流; the visual section says 执行结构.
- Experimental restrictions, synthetic alert local test restriction, and knowledge source review reminder stay visible.
- No new graph dependency and no graph execution changes.

---

### Task 1: Dotted detail route

**Files:** Modify `apps/agent/tests/integration/test_web_assets.py`, `apps/agent/src/workbench_agent/api/frontend.py`.

**Interfaces:** `GET /app/graphs/<installed-graph-id>` with `Accept: text/html` serves `index.html`; unknown IDs, missing assets, and non HTML requests remain 404.

- [ ] **Step 1: Add failing assertions** to `test_spa_deep_links_and_assets`:

```python
assert (await client.get('/app/graphs/conversation.multi_turn', headers={'Accept': 'text/html'})).text == '<html>DoD app</html>'
assert (await client.get('/app/graphs/unknown.graph', headers={'Accept': 'text/html'})).status_code == 404
assert (await client.get('/app/graphs/unknown', headers={'Accept': 'text/html'})).status_code == 404
assert (await client.get('/app/graphs/conversation.multi_turn')).status_code == 404
```

- [ ] **Step 2: Confirm red.** Run `cd apps/agent && uv run python -m pytest tests/integration/test_web_assets.py::test_spa_deep_links_and_assets -q` and confirm the valid route fails with 404.
- [ ] **Step 3: Implement the route exception.** Import `ALL_GRAPH_IDS`; classify a dotted path as an SPA route only if it is exactly `graphs/<id>` and `<id>` is installed. Preserve the existing path traversal, missing asset, and Accept checks.
- [ ] **Step 4: Confirm green.** Run the same test module and `git diff --check`.

### Task 2: Directory hierarchy and naming

**Files:** Modify `apps/web/src/App.vue`, `apps/web/src/views/Graphs.vue`, `apps/web/src/views/Agents.vue`, `apps/web/src/style.css`, `apps/web/tests/graphs-view.test.ts`, `apps/web/tests/app.test.ts`, affected Agent view assertions, `docs/01-product.md`.

**Interfaces:** Catalog retains `GraphApi.list()` data. Recommended and experimental cards precede an expandable comparison table. Each card uses a `router-link` to `graph-detail` and exposes a visible “查看详情” cue.

- [ ] **Step 1: Add failing UI assertions.** In `graphs-view.test.ts`, assert `工作流` appears, the first rendered card appears before `能力对比`, and each graph has a detail cue. In `app.test.ts`, assert navigation contains `工作流`.
- [ ] **Step 2: Confirm red.** Run `cd apps/web && npm test -- graphs-view.test.ts app.test.ts`.
- [ ] **Step 3: Implement the catalog.** Put card groups before a `<details>` comparison panel; make each card link large and visually clear. Show status, entrypoint, usage, and required restrictions using existing catalog fields. Change the Agent selector's graph related copy to 工作流 without changing its field or request payload. Add scoped CSS for cards and responsive spacing. Update product documentation's catalog heading and user visible terminology.
- [ ] **Step 4: Confirm green.** Run `cd apps/web && npm test -- graphs-view.test.ts app.test.ts agents-view.test.ts` and `npm run typecheck`.

### Task 3: B style detail and node inspector

**Files:** Modify `apps/web/src/views/Graphs.vue`, `apps/web/src/components/GraphView.vue`, `apps/web/src/graph-layout.ts`, `apps/web/src/style.css`, `apps/web/tests/graphs-view.test.ts`, `apps/web/tests/graph-view.test.ts`, `apps/web/tests/graph-layout.test.ts`.

**Interfaces:** `GraphView` still accepts `{nodes, edges}`. It renders the SVG canvas beside a node inspector and defaults to the first non synthetic node. SVG node click and Enter/Space selection update the inspector.

- [ ] **Step 1: Add failing behavior tests.** In `graph-view.test.ts`, mount the real component with two nodes, verify the first actual node summary appears in the inspector, select the second node, and verify its reads/writes/cost replace the first. Assert labelled conditional and loop edges remain. In `graphs-view.test.ts`, assert detail includes the named execution section before secondary information.
- [ ] **Step 2: Confirm red.** Run `cd apps/web && npm test -- graph-view.test.ts graphs-view.test.ts`.
- [ ] **Step 3: Implement B layout.** Make the SVG and inspector sibling panels in a responsive grid. Put each node's type in the SVG as text, provide selected outlines and an accessible node button, route skip-layer edges around intervening nodes, and keep edge labels readable. Move applicability, steps and technical ID below the graph; put critical restrictions near the detail title. Avoid duplicate node explanation lists.
- [ ] **Step 4: Confirm green.** Run the focused tests, `npm run typecheck`, and `npm run build`.

### Task 4: End to end review

**Files:** No required new files; correct only defects found in the preceding tasks.

- [ ] **Step 1: Run full checks.** Run `cd apps/web && npm test && npm run typecheck && npm run build`; run `cd apps/agent && uv run python -m pytest tests/integration/test_web_assets.py -q`.
- [ ] **Step 2: Inspect in a browser.** Verify the directory, the multi turn conversation detail, SOP conditional edges and loop, narrow layout, and HTML detail refresh. Use the current built server or a local preview and report whether the running port 8000 process is serving the new build.
- [ ] **Step 3: Review diff and commit.** Run `git diff --check`, inspect `git status --short`, and commit the verified implementation on `codex/workflow-catalog-ui`.

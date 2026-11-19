# Semantic Graph IDs and Graph Catalog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 清理 11 张早期图，将保留的 8 张图改为稳定的语义化 ID，并让全部 8 张都能在管理端查看和绑定。

**Architecture:** 先退役不再注册的 11 张图，再在代码、API、配置和持久化协议中统一 `graph_id`，完成一次清库后的无兼容切换。Python 代码目录记录 8 张图的展示元数据，从编译后的 LangGraph 提取结构并缓存；FastAPI 提供团队内只读接口，Vue 管理端展示并绑定全部 8 张图。保留图的执行逻辑不因改名而改变。

**Tech Stack:** Python 3.12、FastAPI、Pydantic 2、LangGraph、SQLAlchemy/Alembic、pytest；Vue 3、TypeScript、Vite、Vitest、内联 SVG。

## Global Constraints

- 原方案：`/Users/xianguiwang/Downloads/workbench-graph-catalog-plan.md`。本计划覆盖其中“保留 dod.vN”的旧决定，以用户最新要求为准。实施基线为 `c5f93fb`，已新增 `dod.v19`。
- 退役 `dod.v1`–`dod.v11` 共 11 张图；保留 `dod.v12`–`dod.v19` 共 8 张。保留图全部改名；代码字段、API JSON、Run 快照与事件、Worker 能力和配置、数据库字段统一使用 `graph_id` 或复数 `graph_ids`。不要提供 `dod.vN` 兼容别名；用户会清库重建。
- 已发布 Profile 的 `version`、LangGraph 的 `state_schema_version` 和检查点 codec 版本是不同概念，保留其现有版本机制。语义化 `graph_id` 仍是不可变实现标识：行为不兼容的未来图使用新 ID，不覆盖旧 ID。
- 全部 8 张图在目录默认列表中公开，可查看和绑定：`conversation.single_turn`、`conversation.multi_turn` 为推荐；其余 6 张为实验。实验状态不改变现有 Run 受理规则。
- `rca.synthetic`、`rca.proposal`、`rca.batch_proposal` 目前只能由合成告警连接器入口触发，不能从普通 Run API 手动运行；管理端选项与详情必须清楚提示。当前适配器只用于本地集成测试，不能宣称已支持生产告警接入。
- `knowledge.citation_review` 对应现有 v19，管理端公开并标为实验；开发文档要求真实知识源与范围隔离核对后才能在真实环境启用，页面必须显示此提醒。
- 新 Agent 默认图为 `conversation.multi_turn`，新 Agent 草稿的 `allowed_modes` 明确设为 `('qa',)`；不要全局改变 `ProfileDraft.allowed_modes` 的默认值。
- 目录列表默认返回全部 8 张图；不保留没有用途的 `include=all` 分支。UI 新选择也含全部 8 张。服务端拒绝所有已退役和未知 ID；目录 `status` 只有 `recommended` 与 `experimental`。
- 默认 UI 只显示中文 `title`。语义 ID 只在图详情“高级信息”、API 和排障日志中显示。
- 不增加图形依赖；不改图执行实现、不做在线编辑或运行轨迹叠加。
- 每个代码任务先写能证明行为的测试，确认预期失败，再最小实现、重跑测试。已有行为的清单核对属于表征测试，可直接通过。
- 三个交付点：PR-1 为旧图清理及全栈 ID/字段切换；PR-2 为后端目录、接口和校验；PR-3 为前端目录与文档。每个 PR 都须通过相应完整门禁；不创建 PR 时按同样顺序形成三个可评审提交组。

## 保留图与命名映射

ID 用小写英文 `领域.能力或形态`，不包含发布顺序数字。下表是全仓替换和测试的唯一对照；代码中以 `GraphId` 常量引用，不再复制字符串字面量。`v1`–`v11` 不提供新 ID，也不进入目录。

| 旧 ID | 新 graph_id | 代码实现 | 目录状态 |
| --- | --- | --- | --- |
| `dod.v12` | `sop.conditional` | `sop_graph.py` 条件分支 | experimental |
| `dod.v13` | `rca.synthetic` | `rca_synthetic.py` | experimental |
| `dod.v14` | `rca.proposal` | `rca_proposal.py` | experimental |
| `dod.v15` | `rca.batch_proposal` | `rca_batch.py` | experimental |
| `dod.v16` | `knowledge.scoped_excerpt` | `qa_v16.py` | experimental |
| `dod.v17` | `conversation.single_turn` | `direct_qa.py` | recommended |
| `dod.v18` | `conversation.multi_turn` | `conversation_qa.py` | recommended |
| `dod.v19` | `knowledge.citation_review` | `knowledge_qa.py` | experimental |

退役清单：`v1` 检查点底座、`v2` 中断底座、`v3` 公开输入演示、`v4` 早期综合图、`v5` 初版 QA、`v6` 代码分析原型、`v7` 多专家原型、`v8` 摘录原型、`v9` RCA 范围原型、`v10` RCA 基线原型、`v11` 初版 SOP。清库后无需保留这些图的运行注册；保留通用的检查点、等待、恢复和执行框架。

## 文件与协议地图

| 责任 | 文件 |
| --- | --- |
| 8 个常量和 ID 集合 | 新建 `apps/agent/src/workbench_agent/runtime/graphs/ids.py` |
| 图注册、条件分支、执行与恢复 | `runtime/graphs/registry.py`、`application/{runs,results,resume,actions,action_authority,connector_authority}.py`、`jobs/{worker,reconciler,deadlines}.py` 及 `rg` 命中的其他运行路径 |
| Profile、Run 快照、Worker 能力 | `domain/{profiles,context,guards}.py`、`application/{profiles,context,bootstrap}.py`、`settings.py`、`persistence/backends/{jobs,actions}.py`、`persistence/events.py` |
| 新数据库结构和迁移 | 新建 `persistence/schema_agent_v5.py`、`persistence/agent_migrations/versions/0005_graph_id.py`；修改 `persistence/schema.py`、`persistence/sqlite_diagnostics.py` |
| 配置和前端旧字段 | `config/local/agent.example.yaml`、`apps/web/src/views/Agents.vue` 及对应测试 |
| 目录声明、结构提取、接口 | 新建 `runtime/graphs/catalog.py`、`api/graphs.py`；修改 `api/app.py` |
| 前端客户端、布局、视图 | 新建 `apps/web/src/{graphs-api.ts,graph-layout.ts}`、`components/GraphView.vue`、`views/Graphs.vue`；修改 `views/Agents.vue`、`router.ts`、`App.vue` |
| 文档 | `docs/01-product.md`、`03-development.md`、`06-reference/{01-api,02-data-dictionary,03-configuration}.md` |

目标协议：

```python
class ProfileDraft(FrozenModel):
    graph_id: str = 'conversation.multi_turn'

class RunContext(FrozenModel):
    graph_id: str

class WorkerCapabilities:
    graph_ids: tuple[str, ...] = ()
    projection_graph_ids: tuple[str, ...] = ()

# settings.py / YAML: worker.supported_graph_ids
# runtime_profiles DB column: graph_id
# RunContext JSON and run.started event payload: graph_id
```

## PR-1：旧图清理与全栈命名切换

### Task 0: 退役 11 张旧图并保住运行底座测试

**Files:** Modify `apps/agent/src/workbench_agent/runtime/graphs/registry.py`, `application/{runs,results,resume,schedules,foundation_context,profiles}.py`, `domain/profiles.py`, `jobs/{worker,reconciler,deadlines}.py`, `runtime/results.py`, `settings.py`; remove unused old graph modules only after import search; modify old图专用测试及 `apps/agent/tests/contract/test_package_layout.py`.

**Interfaces:** Worker 只注册 v12–v19；旧 v1–v11 不能运行，保存和发布时的明确 422 校验在 Task 6 完成。通用 `FoundationExecutor`、`InterruptExecutor`、检查点、等待和恢复机制继续由保留图使用。

- [ ] **Step 1: 写失败清单测试** `apps/agent/tests/contract/test_installed_graphs.py`：注册集合恰为 `{dod.v12,...,dod.v19}`；旧 v1–v11 均不能解析；新建 Agent 草稿默认 `dod.v18` 且仅允许 `qa`。运行目标测试，确认当前仍注册 19 图而失败。

- [ ] **Step 2: 逐个撤掉旧注册与专属分支。** `registry.py` 只注册 8 图；`sop_graph.py` 默认构造切到 v12，保留 v12 条件分支；`schedules.py` 只接受 v12。删除 v1–v11 的特殊受理、结果和恢复判断，但保留 v13–v15 合成告警及 v12 SOP 路径。将 `ProfileDraft` 默认图暂设 `dod.v18`，新建 Agent 草稿明确 `allowed_modes=('qa',)`，Worker 默认支持 v17/v18。`rg -n 'dod\.v([1-9]|10|11)([^0-9]|$)' apps/agent/src` 用于核对每处残留。

- [ ] **Step 3: 整理测试，不以删除失败测试掩盖覆盖缺口。** 旧图专用业务测试可删除；测试通用幂等、租约、检查点、等待、恢复、预算、跨团队隔离的用例应换成 v12 或 v13–v19 的真实场景。若某个底座边界只由旧图触发，将最小测试图放进 `apps/agent/tests/support/` 并由测试私有注册表安装，生产注册表仍只有 8 图。逐个运行受影响的 `tests/contract`、`tests/integration`、`tests/fault`。

- [ ] **Step 4: 删除已无生产或测试导入的旧图模块及孤立工具。** 使用 `rg` 核对依赖后删文件；保留 v13–v15 可能复用的 RCA 工具函数。运行 `cd apps/agent && uv run python -m pytest -q -ra --durations=25`，确认旧图删除没有损坏剩余路径。

- [ ] **Step 5: 提交** `git add apps/agent && git commit -m "refactor: retire eleven obsolete graphs"`。

### Task 1: 8 个语义 ID 与运行分派

**Files:** Create `apps/agent/src/workbench_agent/runtime/graphs/ids.py`; modify every production Python file returned by `rg -l 'dod\.v[0-9]+' apps/agent/src`; modify matching tests in `apps/agent/tests`; modify `config/local/agent.example.yaml` after Task 2 changes the settings key.

**Interfaces:** `GraphId(StrEnum)` contains exactly the 8 retained values above; `ALL_GRAPH_IDS = frozenset(GraphId)`。图注册、运行受理、动作授权、Worker 和 Reconciler 用枚举常量比较；对外仍序列化为字符串。

- [ ] **Step 1: 写失败契约测试** `apps/agent/tests/contract/test_graph_ids.py`：8 张保留图各有唯一新 ID、`foundation_worker_registry(...).capabilities().graph_versions` 的集合恰等于 8 个新 ID、任一旧 `dod.vN` 无法解析。先运行 `cd apps/agent && uv run python -m pytest tests/contract/test_graph_ids.py -q`，确认缺少 ID 定义或注册而失败。

- [ ] **Step 2: 定义 `GraphId`，按映射更新 `runtime/graphs/registry.py`。** 同步 `sop_graph.py` 中决定条件分支的旧 ID 判断和图内其他旧 ID 比较，不改变节点、连线或状态 Schema ID。运行契约测试，确认注册集合为 8 个。

- [ ] **Step 3: 更新所有业务分支。** 逐文件处理 `application`、`jobs`、`runtime`、`incidents`、`delivery`、`knowledge`、`observability`、`tools` 中的 `dod.vN`；每处理一组，运行该组已有集成测试。对 `RunService`、Reconciler 的允许节点表、合成告警入口、SOP、知识问答、对话历史边界分别添加参数化对照断言，确保仅名称变化。

- [ ] **Step 4: 更新测试数据与断言中保留的 8 个旧取值。** 不替换 `dod-state-vN`、检查点 codec、历史文档引用。运行 `cd apps/agent && uv run python -m pytest -q -ra --durations=25`。如果失败，按旧 ID 用例定位遗漏，不加入运行时兼容别名。

- [ ] **Step 5: 提交** `git add apps/agent/src apps/agent/tests && git commit -m "refactor: name all graph implementations semantically"`。

### Task 2: `graph_version` 协议统一改为 `graph_id`

**Files:** Modify `apps/agent/src/workbench_agent/domain/{profiles,context,guards}.py`, `application/{profiles,context,bootstrap}.py`, `jobs/{worker,claim}.py`, `settings.py`, `persistence/backends/{jobs,actions}.py`, `persistence/events.py` and every other `rg -l 'graph_version' apps/agent/src` result; create `persistence/schema_agent_v5.py`, `persistence/agent_migrations/versions/0005_graph_id.py`; modify `persistence/schema.py`, `persistence/sqlite_diagnostics.py`, `config/local/agent.example.yaml`; update affected Python tests.

**Interfaces:** `graph_version → graph_id`；`graph_versions → graph_ids`；`projection_graph_versions → projection_graph_ids`；`supported_graph_versions → supported_graph_ids`。原字段不再接受。`state_schema_version`、Profile 的发布 `version` 保持原样。

- [ ] **Step 1: 写失败序列化和迁移测试** `apps/agent/tests/contract/test_graph_id_protocol.py`、`apps/agent/tests/integration/test_graph_id_migration.py`：`ProfileDraft`/`RunContext` 只序列化 `graph_id` 且拒绝旧键；Worker 能力仅含 `graph_ids`；默认 Worker 支持推荐的两张对话图，六张实验图需要部署显式启用；新建空库迁移到 `0005_graph_id` 后 `runtime_profiles` 只有 `graph_id` 列。先运行两文件，确认旧字段仍存在导致失败。

- [ ] **Step 2: 添加 v5 schema 和 Alembic 迁移。** 从 v4 元数据建立新快照，把 `runtime_profiles.graph_version` 改为 `graph_id`；不修改 v1–v4 快照。迁移前检查 `runtime_profiles`、`runs`、`jobs` 为空，非空时明确失败并提示先清库；然后用 SQLite batch alter 重命名列。更新当前 schema 入口、迁移头、健康检查和结构对照测试。Worker 配置沿用 Task 0 选定的两张推荐对话图作为默认；其他六张在部署启用对应能力后加入 `supported_graph_ids`。

- [ ] **Step 3: 修改所有 Python 字段与 JSON 键。** 包括 `RunContext` 冻结快照、`runtime_profiles` 存储、`runs.versions` 的 `$.graph_id` 查询、`run.started` 事件、Worker 能力、配置解析、追踪属性及所有调用方。全仓生产代码搜索 `graph_version`，除明确讨论旧协议的迁移/文档外不得残留。

- [ ] **Step 4: 更新所有测试构造和断言。** 特别检查备份恢复、故障、跨团队、调度和合成告警用例中的 JSON 快照；运行 `cd apps/agent && uv run python -m pytest tests/contract/test_graph_id_protocol.py tests/integration/test_graph_id_migration.py -q`，再运行完整 Agent 测试。

- [ ] **Step 5: 提交** `git add apps/agent config/local/agent.example.yaml && git commit -m "refactor: use graph_id throughout agent protocol"`。

### Task 3: 管理端字段适配与命名切换门禁

**Files:** Modify `apps/web/src/views/Agents.vue`, `apps/web/tests/agents-view.test.ts`, `apps/web/tests/agents-api.test.ts`, `apps/web/src/views/Threads.vue`, `apps/web/tests/threads-view.test.ts`; modify `docs/06-reference/03-configuration.md` as part of PR-1 release notes.

**Interfaces:** Agent PATCH/GET 的字段从 `graph_version` 改为 `graph_id`，下拉暂沿用当前静态选项但值改为语义 ID；`knowledge.citation_review` 的来源有效期设置保留。

- [ ] **Step 1: 写失败前端测试。** Agent 编辑后 PATCH 正文含 `graph_id` 而无 `graph_version`；当前三项图使用新 ID；Threads 错误文案不显示旧 `dod.v18`。运行 `cd apps/web && npm test -- agents-view.test.ts agents-api.test.ts threads-view.test.ts`，确认旧字段/旧值导致失败。

- [ ] **Step 2: 更新页面解析、下拉和表单提交。** 不改变其他草稿字段；更新 config 文档中的 `worker.supported_graph_ids` 和默认三张 foundation 图。运行 `cd apps/web && npm test && npm run typecheck && npm run build`。

- [ ] **Step 3: PR-1 门禁。** 重新运行完整 Agent 测试、Web 门禁和 `python3 scripts/check_docs.py`；搜索 `rg -n 'dod\.v[0-9]+|graph_version' apps/agent/src apps/web/src config/local`，结果只能是明确的迁移前置检查或历史注释。以真实测试结果修正遗漏。

- [ ] **Step 4: 提交** `git add apps/web docs/06-reference/03-configuration.md && git commit -m "refactor: use semantic graph IDs in management UI"`。

## PR-2：后端图目录

### Task 4: 元数据注册与真实结构提取

**Files:** Create `apps/agent/src/workbench_agent/runtime/graphs/catalog.py`, `apps/agent/tests/unit/test_graph_catalog.py`.

**Interfaces:** 原方案 §2.1 的 `GraphSpec`、`NodeInfo`、`GraphCapabilities`，但 `status` 仅 `recommended|experimental`，能力增加 `entrypoints: tuple[Literal['manual','schedule','synthetic_alert'], ...]`；`list_graphs()`、`get_graph(graph_id)`、`graph_structure(graph_id)`。API 边形状为 `{source,target,conditional,label}`；结构仅按 graph_id 缓存，不缓存团队数据。

- [ ] **Step 1: 写失败测试。** 8 个 `GraphId` 都有规格，列表恰为这 8 张；每张图的 `title/one_liner/when_to_use/when_not_to_use/steps_text` 非空；真实节点与 `NodeInfo` 一致；每条条件边有中文说明；`sop.conditional` 有带文字的 `step → step` 自环。三张 RCA 图的 `entrypoints` 只有 `synthetic_alert`，SOP 为 `manual/schedule`，其余为 `manual`。运行 `cd apps/agent && uv run python -m pytest tests/unit/test_graph_catalog.py -q`，确认目录缺失。

- [ ] **Step 2: 逐图核对源码填写文案和能力。** `conversation.single_turn` 不使用工具或历史；`conversation.multi_turn` 使用最多 8 轮历史；`knowledge.scoped_excerpt` 需要知识工具和查询范围且结果恒为部分完成；`sop.conditional` 需要工作流。三张 RCA 图说明各自的合成告警、提案或批量提案能力及“仅合成入口、本地测试适配器”限制。`knowledge.citation_review` 说明来源有效期、引文校验和真实环境未验收，不宣称结论已可信。

- [ ] **Step 3: 以无检查点的图构造函数编译 8 张图并调用 `get_graph()`。** `sop.conditional` 的构造必须传对应语义 ID 以保留条件分支；从编译结果提取节点、边和条件标记，文字只从 `edge_labels` 注入。重复调用应命中结构缓存。运行目录单测和 `tests/contract/test_graph_ids.py`。

- [ ] **Step 4: 提交** `git add apps/agent/src/workbench_agent/runtime/graphs/catalog.py apps/agent/tests/unit/test_graph_catalog.py && git commit -m "feat: describe installed graphs and extract structure"`。

### Task 5: 只读图接口与团队使用量

**Files:** Create `apps/agent/src/workbench_agent/api/graphs.py`, `apps/agent/tests/integration/test_graph_catalog_api.py`; modify `apps/agent/src/workbench_agent/api/app.py`.

**Interfaces:** `GET /v1/graphs` 返回全部 8 张的 `{items}`；`GET /v1/graphs/{graph_id}` 返回详情。列表含 `graph_id,title,summary,status,capabilities,agents_using`；详情再含适用/不适用、步骤、备注、节点与边。权限使用 `authenticate` 和 `Authorization.current(..., minimum='viewer')`。

- [ ] **Step 1: 写失败 API 测试。** 列表恰有 8 张且全部可读、SOP 详情含条件边与自环、三张 RCA 详情明确 `synthetic_alert` 入口、未知或已退役 ID 404、匿名 401、撤销成员 404。为两团队分别发布一张相同图，断言本团队 `agents_using == 1`；连接器有 `agent_scope` 时只统计其范围内 Agent。运行目标测试，确认路由缺失。

- [ ] **Step 2: 实现只读路由。** `agents_using` 用 SQL `COUNT` 连接 active Agent 与 active published profile，同时过滤 `team_id` 和可选 `agent_scope`，避免 `configuration.rows(limit=100)` 截断。目录结构缓存不含使用量。

- [ ] **Step 3: 运行** `cd apps/agent && uv run python -m pytest tests/integration/test_graph_catalog_api.py tests/integration/test_tenant_isolation.py -q`；提交 `git add apps/agent/src/workbench_agent/api apps/agent/tests/integration/test_graph_catalog_api.py && git commit -m "feat: expose team graph catalog"`。

### Task 6: Agent 保存与发布校验、默认图

**Files:** Modify `apps/agent/src/workbench_agent/application/profiles.py`, `apps/agent/src/workbench_agent/domain/profiles.py`; create `apps/agent/tests/integration/test_graph_profile_validation.py`, `apps/agent/tests/integration/test_graph_catalog_admission.py`; adjust tests whose implicit default was `foundation.public_input`.

**Interfaces:** `save_draft()` 和 `publish()` 在幂等操作内验证 `graph_id` 属于 8 张图；任何已退役或未知 ID 返回 `DomainError('GRAPH_UNAVAILABLE', 422)`。八张图均可保存和发布；Run 受理仍按入口及模式规则执行。

- [ ] **Step 1: 写失败测试。** PATCH 未知或已退役 ID 返回 422 且 revision 不变；非法草稿发布返回相同错误且 active profile 不变；新 Agent 草稿为 `conversation.multi_turn` 和 `('qa',)`；八张图逐一可保存及发布。

- [ ] **Step 2: 写受理一致性测试。** 以真实 `RunService` 对照八张图的 `capabilities.modes/entrypoints`：对话和知识图仅 `qa`，SOP 仅 `sop/inspection`，三张 RCA 仅合成告警入口的 `rca`；知识图提供合法 `query_scope`，SOP 提供已发布 `workflow_version_id`；声明外模式及从普通 Run API 手动触发 RCA 必须被拒。运行两组测试，确认校验功能的红灯。

- [ ] **Step 3: 在 `save_draft`/`publish` 写入前执行目录校验。** 核对 Task 0 的新建 Agent 默认图在改名后仍为 `conversation.multi_turn` 且 `allowed_modes=('qa',)`；仅把依赖旧默认值的测试改为显式图 ID。

- [ ] **Step 4: 运行目标测试与完整 Agent 测试。** 提交 `git add apps/agent/src/workbench_agent/application/profiles.py apps/agent/src/workbench_agent/domain/profiles.py apps/agent/tests && git commit -m "feat: validate agent graph selection"`。

- [ ] **Step 5: PR-2 门禁。** `cd apps/agent && uv run python -m pytest -q -ra --durations=25`，再核对 8 图全部公开、422 校验、入口限制和团队隔离的验收断言。

## PR-3：前端目录与文档

### Task 7: 目录客户端与纯布局函数

**Files:** Create `apps/web/src/graphs-api.ts`, `apps/web/src/graph-layout.ts`, `apps/web/tests/graphs-api.test.ts`, `apps/web/tests/graph-layout.test.ts`.

**Interfaces:** `GraphApi.list(): Promise<GraphSummary[]>`、`GraphApi.detail(graphId: string): Promise<GraphDetail>`；`layoutGraph(nodes, edges)` 返回稳定坐标和 SVG 边几何，不依赖 DOM。

- [ ] **Step 1: 写失败测试。** 客户端覆盖八图列表、详情 ID URL 编码、401/404 错误传播；布局覆盖线性、条件边、自环、孤立节点与重复输入，断言坐标有限、节点不重叠、结果稳定、自环有独立路径和文字锚点。运行 `cd apps/web && npm test -- graphs-api.test.ts graph-layout.test.ts`，确认模块缺失。

- [ ] **Step 2: 参照 `agents-api.ts` 实现请求；布局按拓扑层次排序，计算层级时忽略自环。** 同层按稳定 ID 排序；普通/条件边保留后端 label。运行目标测试和 `npm run typecheck`。

- [ ] **Step 3: 提交** `git add apps/web/src/graphs-api.ts apps/web/src/graph-layout.ts apps/web/tests/graphs-api.test.ts apps/web/tests/graph-layout.test.ts && git commit -m "feat: add graph catalog client and layout"`。

### Task 8: SVG 结构图、目录页和路由

**Files:** Create `apps/web/src/components/GraphView.vue`, `apps/web/src/views/Graphs.vue`, `apps/web/tests/graph-view.test.ts`, `apps/web/tests/graphs-view.test.ts`; modify `apps/web/src/router.ts`, `apps/web/src/App.vue`, `apps/web/src/style.css`, `apps/web/tests/router.test.ts`, `apps/web/tests/app.test.ts`.

**Interfaces:** `/graphs` 和 `/graphs/:id` 路由；概览卡、能力对比表、结构图和文字执行过程读取同一 `GraphDetail`。

- [ ] **Step 1: 写失败视图和路由测试。** 默认 8 张全部可查看、状态与使用量、条件文字、自环、图例、节点读/写/消耗、适用/不适用、备注、文字步骤；三张 RCA 卡片明确“仅合成告警入口”，知识引用图明确“真实来源未验收”；语义 ID 只在“高级信息”展开后显示；无 Token、空态和错误态有可读提示。运行目标测试，确认页面不存在。

- [ ] **Step 2: 实现结构图。** 节点按 kind 着色；顺序线为实线、条件线为虚线、自环为回环；节点可键盘聚焦并显示详情。执行过程文字版承担无障碍替代。

- [ ] **Step 3: 实现目录、路由和导航。** 默认展示全部八图，按推荐/实验分组；切换详情 ID 时清除旧数据，避免显示错误图。运行 `cd apps/web && npm test -- graph-view.test.ts graphs-view.test.ts router.test.ts app.test.ts`、typecheck、build。

- [ ] **Step 4: 提交** `git add apps/web/src apps/web/tests && git commit -m "feat: visualize graph catalog"`。

### Task 9: Agent 选图与能力提示

**Files:** Modify `apps/web/src/views/Agents.vue`, `apps/web/src/views/Threads.vue`, `apps/web/tests/agents-view.test.ts`, `apps/web/tests/threads-view.test.ts`.

**Interfaces:** 下拉取 `GraphApi.list()` 的八张公开图，提交 `graph_id`；所有目录图均可绑定，选中实验图时显示运行入口和部署前置条件。

- [ ] **Step 1: 写失败测试。** 八张图名称与摘要均可见并可选；新 Agent 显示“对话（多轮）”；目录请求失败时保留当前图并避免误保存；知识摘录提示知识工具/查询范围，SOP 提示运行时工作流，三张 RCA 提示只能由合成告警入口运行且当前适配器仅供本地测试，六张实验图提示 Worker 需配置对应 graph_id，知识引用图提示真实来源验收；“查看结构”链接正确。

- [ ] **Step 2: 实现目录驱动下拉与黄色能力提示。** 保留 JSON 编辑器未知字段；`knowledge.citation_review` 的 `knowledge_max_age_seconds` 输入和保存能力不丢失。更新 Threads 错误提示为中文名称。

- [ ] **Step 3: 运行** `cd apps/web && npm test -- agents-view.test.ts threads-view.test.ts`、typecheck、build；提交 `git add apps/web/src/views apps/web/tests && git commit -m "feat: choose graphs by catalog name"`。

### Task 10: 文档、命名审计与最终门禁

**Files:** Modify `docs/01-product.md`, `docs/03-development.md`, `docs/06-reference/01-api.md`, `docs/06-reference/02-data-dictionary.md`, `docs/06-reference/03-configuration.md`; modify `docs/README.md` only if directory navigation needs it.

**Interfaces:** 文档列出退役的 11 张图、保留并公开的 8 个语义 ID、两个接口、`graph_id` 字段及清库重建前置条件。

- [ ] **Step 1: 更新产品与开发文档。** 说明推荐/实验的真实含义、八图的运行入口和结果可信度、退役图范围、注册新图时的代码/目录/测试步骤，以及不兼容实现如何使用新的语义 ID。说明三张 RCA 图仅有本地合成告警入口、`knowledge.citation_review` 的真实知识源启用前置条件。

- [ ] **Step 2: 更新 API、数据字典和配置参考。** Agent Profile 与 Run 事件中的 `graph_id`、Worker `supported_graph_ids`、数据库 `runtime_profiles.graph_id`、八图目录响应与权限均以新协议写出；旧 `dod.vN` 只留在退役和迁移对照说明中。

- [ ] **Step 3: 审计代码和用户文案。** 搜索 `rg -n 'dod\.v[0-9]+|graph_version|graph_versions' apps/agent/src apps/web/src config/local`；逐项处理，旧名仅允许出现在拒绝迁移旧数据的检查或明确的历史注释中。检查 Run 详情若显示所用图，显示中文标题。

- [ ] **Step 4: 更新修改文档的 `verified_commit` 并运行** `python3 scripts/check_docs.py --base origin/main`；若无法读取远端基线，运行 `python3 scripts/check_docs.py` 并记录原因。

- [ ] **Step 5: 最终门禁。** 运行 `cd apps/agent && uv run python -m pytest -q -ra --durations=25`；运行 `cd apps/web && npm test && npm run typecheck && npm run build`；运行文档检查。命令全部实际通过后，检查 `git diff --check`、`git status --short` 和三个交付点的提交边界。

- [ ] **Step 6: 提交** `git add docs && git commit -m "docs: document semantic graph IDs and catalog"`。

## 验收清单

- [ ] 生产 Worker 仅注册 8 张保留图，并均使用语义 ID；11 张退役图不能解析，注册、执行、恢复、Worker 能力和测试中没有旧 `dod.vN` 运行取值。
- [ ] Profile/Run/事件/配置/数据库均使用 `graph_id` 或 `graph_ids`；清库迁移到 `0005_graph_id` 后不存在旧列。
- [ ] 8 个已注册图均有目录条目、默认可见且可绑定；`knowledge.citation_review` 与三张 RCA 图标为实验并显示准确的使用限制。
- [ ] 八张图的真实节点与每条条件边都有中文说明；SOP 自环可视且有文字说明。
- [ ] viewer 可读团队目录；匿名、撤销身份和跨团队数据不可见；使用量按当前有效发布配置统计。
- [ ] 未知/已退役 ID 在草稿保存和发布时返回 422；八张图均可配置，RCA 手动 Run 仍按现有规则拒绝。
- [ ] 新 Agent 默认 `conversation.multi_turn` 且允许 `qa`；前端按显示名展示并允许绑定八张图。
- [ ] Agent 全量测试、Web 测试、类型检查、构建和文档检查全部通过。

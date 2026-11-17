---
title: "DoD Agents Python：M4 运营与发布实施计划"
date: "2026-11-12"
status: ready-for-implementation
---

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付管理工作台、可核对的质量与费用、旧资产迁移和经过容量/故障/备份演练的SQLite首版。
**Architecture:** 观测和评估异步旁路且不改主结果；UI消费业务API；唯一入口路由隔离旧新引擎；发布以人工标签和真实故障证据为门槛。
**Tech Stack:** OpenTelemetry、Prometheus、可选Langfuse Python SDK 5.2.0、Vue 3、TypeScript、Vite、Element Plus、Vue Router、Playwright、pytest。

## Global Constraints

遵守[总计划](2026-11-12-workbench-python-implementation-plan.md)。评估不以模型自评分代替人工真值；观测不记录内部思考或凭据；SQLite受理p95≤500ms和恢复≤90s按规格条件实测；AT-30/31不计入P0已完成。

## 版本与参考

- 2026-11-12核对：[Langfuse PyPI 5.2.0](https://pypi.org/project/langfuse/5.2.0/)；[SDK兼容性](https://langfuse.com/docs/observability/sdk/overview)要求Python SDK v5使用Langfuse v4.59.0+。实施时锁定本计划版本；升级需另验收。
- [观测结构指南](https://langfuse.com/docs/observability/best-practices)用于命名、类型和嵌套，数据采集范围以本规格脱敏/不保存内部思考契约为准。
- [数据集指南](https://langfuse.com/academy/datasets)用于离线版本设计；本任务生成本地人工审核集，不自动创建或上传live Langfuse数据集。

## 实施任务

### T21：Trace/指标、费用、反馈、知识缺口和人工标签评估

**依赖与依据：** T20；规格 章节 9.2、13.4、19、23.2；AT-19/20/36。
**文件：** 创建或修改 `src/observability/tracing.py`、`src/observability/metrics.py`、`src/observability/redaction.py`、`src/observability/langfuse_adapter.py`、`src/evaluation/feedback.py`、`src/evaluation/gaps.py`、`src/evaluation/datasets.py`、`src/evaluation/report.py`、`src/jobs/retention.py`、`src/api/feedback.py`、`src/api/usage.py`、`src/cli/evaluate.py`、`tests/integration/test_feedback_evaluation.py`、`tests/integration/test_usage.py`、`tests/contract/test_observability.py`、`tests/integration/test_retention.py`、`tests/fixtures/business/manifest.json`、`docs/acceptance/evaluation.md`、`tests/support/evaluation.py`。
**Interfaces：** FeedbackService.record(auth, run_id, rating, comment, key) -> Feedback；EvaluationService.evaluate(case: ReviewedCase, outcome: RunOutcome) -> EvaluationResult；KnowledgeGapService.record(retrieval_trace, kind) -> KnowledgeGap；RetentionService.sweep(now_ms, batch_size) -> RetentionReport；ObservabilityAdapter.record(event) -> None。

- [ ] **Step 1：写失败测试及本任务列明的边界用例。**

```python
import pytest

@pytest.mark.asyncio
async def test_evaluation_failure_cannot_change_main_answer(platform):
    run = await platform.completed_run()
    before = await platform.get_run(run.run_id)
    platform.evaluator.raise_unavailable()
    await platform.run_post_evaluation(run.run_id)
    after = await platform.get_run(run.run_id)
    assert after.status == before.status
    assert after.outcome == before.outcome
    assert await platform.evaluation_state(run.run_id) == "unavailable"
```

- [ ] **Step 2：运行测试确认失败。**

```bash
uv run pytest tests/integration/test_feedback_evaluation.py tests/integration/test_usage.py tests/contract/test_observability.py tests/integration/test_retention.py -v
```

预期首次因尚未实现的导入、接口或目标行为失败；夹具自身报错先修复夹具，不能把无关失败当红灯。

- [ ] **Step 3：实现最小交付。**

OTel关联request→Run/Job attempt→Graph/expert→model/tool/Runner/Action；一个Run对应稳定Trace，attempt用子Span区分并保留run_id关联，多轮用业务thread session关联；每次模型尝试generation、专家agent、知识retriever、工具tool，正确嵌套且不重复代理span。输入输出仅脱敏必要摘要，明确排除内部思考；该规则优先于外部观测指南的默认采集建议。Prometheus实现规格全部指标，不以run_id为label。费用主/专家/重试/评估独立明细且汇总同Run，unknown保持pending。Langfuse可选适配计划锁定langfuse==5.2.0（2026-11-12核对PyPI最新稳定）；该SDK要求服务端v4.59.0+，部署先验兼容，未满足时使用OTLP/本地观测并明确Langfuse适配未验收，不能默用旧API。配置读取secret_ref，不把SDK变核心执行依赖；发送真实脱敏Trace后通过Observations API v2/CLI获取回查并审查结构，记录Trace链接与版本。反馈追加数据库后异步映射score，失败不丢本地反馈。Knowledge Gap仅依据真实retrieval轨迹区分未命中/冲突/过期/服务不可用，评估不能改主回答。实现小批量retention及引用保护/删除审计，Evidence原文过期UI可见。数据集使用30条人工审核manifest，记录reviewer/根因/引用/可用性；评估report计算固定分母、缺标签/缺输出计缺失不能选择性剔除，模型自评不当真值。tests/support/evaluation.py（本任务新增）提供completed_run/evaluator/run_post_evaluation/evaluation_state及四类检索记录；生成标签只可草稿。

关键实现约束：

```python
def release_quality_passes(report) -> bool:
    return (
        report.reviewed_cases >= 30
        and report.confirmed_root_cause_cases >= 10
        and report.evidence_reference_accuracy >= 0.95
        and report.human_usable_answer_ratio >= 0.80
        and report.primary_root_cause_match_ratio >= 0.80
        and report.unavailable_reported_as_normal == 0
        and report.unauthorized_access_misses == 0
    )
```

- [ ] **Step 4：执行上述测试命令及已完成的相关契约，确认通过。**

通过条件：评估失败不改主结果、费用不丢失、脱敏和指标基数通过；人工标签质量报告及真实Langfuse回查分别记录，通过与未验收不能混写。 保留实际执行输出到阶段验收记录。

- [ ] **Step 5：检查差异并提交当前任务。**

```bash
git diff --check
git add -- src/observability/tracing.py src/observability/metrics.py src/observability/redaction.py src/observability/langfuse_adapter.py src/evaluation/feedback.py src/evaluation/gaps.py src/evaluation/datasets.py src/evaluation/report.py src/jobs/retention.py src/api/feedback.py src/api/usage.py src/cli/evaluate.py tests/integration/test_feedback_evaluation.py tests/integration/test_usage.py tests/contract/test_observability.py tests/integration/test_retention.py tests/fixtures/business/manifest.json docs/acceptance/evaluation.md tests/support/evaluation.py
git diff --cached
git commit -m "feat: add accountable observability feedback and evaluation"
```

仅包含本任务交付文件；某项文件未改动时从暂存命令去掉，不夹带其他项目内容。


### T22：可操作的管理工作台与真实状态页面

**依赖与依据：** T21；规格 章节 4.3、11、17、20；AT-03/20。
**文件：** 创建或修改 `src/web/routes.py`、`web/package.json`、`web/package-lock.json`、`web/tsconfig.json`、`web/vite.config.ts`、`web/index.html`、`web/src/`（Vue 页面、组件、路由、API 客户端、SSE 解析及样式）、`src/api/session.py`、`tests/support/ui.py`、`tests/e2e/test_admin_ui.py`、`tests/integration/test_tenant_isolation.py`。
**Interfaces：** GET /app 下页面仅消费已授权 /v1 DTO，不读检查点；OIDC SessionResolver -> AuthContext；RunStreamReducer.apply(event: RunEvent) -> UiRunState，按seq/attempt/message_id合并。

- [ ] **Step 1：写失败测试及本任务列明的边界用例。**

```python
import pytest

@pytest.mark.asyncio
async def test_ui_distinguishes_analysis_and_unknown_delivery(browser_page, ui_seed):
    run = await ui_seed.completed_run_with_unknown_action()
    await browser_page.goto(f"{ui_seed.base_url}/app/runs/{run.run_id}")
    await browser_page.get_by_text("分析完成", exact=True).wait_for()
    await browser_page.get_by_text("外部动作结果待核对", exact=True).wait_for()
    assert await browser_page.get_by_text("Jira 已创建", exact=True).count() == 0
```

- [ ] **Step 2：运行测试确认失败。**

```bash
uv run pytest tests/e2e/test_admin_ui.py tests/integration/test_tenant_isolation.py -v
```

预期首次因尚未实现的导入、接口或目标行为失败；夹具自身报错先修复夹具，不能把无关失败当红灯。

- [ ] **Step 3：实现最小交付。**

FastAPI 静态资源和 SPA 回退代码位于 src/web/，Vue 3 + TypeScript + Vite + Element Plus + Vue Router 前端项目位于根目录 web/。使用受支持的 Node.js LTS 与 npm ci 按 package-lock.json 安装，执行类型检查和构建；Vite base 与 Vue Router history 均配置为 /app/。部署镜像将 wheel 和 web/dist/ 复制到 WORKBENCH_ASSET_ROOT，运行镜像无需 Node.js；切换工作目录后验证 /app/、深层页面刷新、静态资源加载，缺失资源返回 404，/v1 和 SSE 不进入 SPA 回退。开发服务器将 /v1 及认证请求代理到同源后端。首版前端覆盖工作台、Run详情、Thread、Workspace、模型、Skill、工具、Schedule、Incident、统计十类用户任务。模型/Workspace草稿发布和回滚、工具配置/发现/调用分层、Skill导入Job、固定commit、等待输入/具名确认/取消、Artifact受控下载、费用pending、独立动作回执真实可操作。Session为服务端OIDC可信主体，浏览器用HttpOnly/Secure/SameSite cookie与CSRF保护或带Bearer的fetch，不能在localStorage保存生产Token；SSE用fetch流解析和同源认证重连。revision冲突刷新并提示；seq去重、attempt草稿superseded与最终答案分开，超期游标显示结果链接；不显示虚假百分比。运行结果/工具文本/Skill源码按文本安全展示，Markdown 使用经过消毒的安全渲染。tests/support/ui.py（本任务新增）定义browser_page与ui_seed，真实API创建测试记录，不直接伪造DOM。用Playwright浏览器测试完整用户操作与无权限按钮/接口均拒绝，数据库断开/等待/unknown/费用缺失/空列表错误态都有呈现。

关键实现约束：

```typescript
export function applyEvent(state: UiRunState, event: RunEvent): UiRunState {
  if (event.seq <= state.lastSeq) return state;
  const next = {...state, lastSeq: event.seq};
  if (event.type === "run.completed") {
    next.analysisStatus = event.payload.terminal_status;
  }
  if (event.type === "action.updated") {
    next.actions = {...state.actions,
      [event.payload.action_id]: event.payload};
  }
  return next;
}
```

- [ ] **Step 4：执行上述测试命令及已完成的相关契约，确认通过。**

前端执行 `npm ci --prefix web`、`npm run typecheck --prefix web`、`npm run build --prefix web`，并运行上述 Playwright/pytest 测试。通过条件：十类页面能操作真实API，SSE重连和具名恢复成功；分析完成、等待与投递待核对状态清晰独立；/app 深层路由刷新、缺失静态资源、/v1 与 SSE 路由隔离通过。保留实际执行输出到阶段验收记录。

- [ ] **Step 5：检查差异并提交当前任务。**

```bash
git diff --check
git add -- src/web/routes.py web/package.json web/package-lock.json web/tsconfig.json web/vite.config.ts web/index.html web/src src/api/session.py tests/support/ui.py tests/e2e/test_admin_ui.py tests/integration/test_tenant_isolation.py
git diff --cached
git commit -m "feat: provide an authenticated DoD operations workbench"
```

仅包含本任务交付文件；某项文件未改动时从暂存命令去掉，不夹带其他项目内容。


### T23：旧资产导入、版本保留与唯一引擎切流

**依赖与依据：** T22；规格 章节 18.3、21.1/21.2；AT-22/24。
**文件：** 创建或修改 `src/migration/legacy_assets.py`、`src/migration/mapping.py`、`src/migration/engine_router.py`、`src/cli/import_legacy.py`、`src/ingress/engine_routing.py`、`src/persistence/migrations/versions/0003_legacy_routes.py`、`tests/integration/test_legacy_import.py`、`tests/integration/test_engine_cutover.py`、`tests/fault/test_version_retention.py`、`docs/runbooks/legacy-cutover.md`、`docs/acceptance/legacy-mapping.md`、`tests/support/migration.py`。
**Interfaces：** LegacyAssetImporter.preview(source_ref) -> ImportManifest；apply(auth, manifest_hash) -> ImportReport；EngineRouter.assign(source_key, published_route_version) -> EngineAssignment；VersionRetention.can_remove(version_ref) -> bool。单一入口路由权威记录source_event→engine。

- [ ] **Step 1：写失败测试及本任务列明的边界用例。**

```python
import pytest

@pytest.mark.asyncio
async def test_source_event_keeps_one_engine_after_route_rollout(platform):
    event = platform.source_event("alert-cutover-1")
    first = await platform.engine_router.assign(event, route_version="legacy-1")
    await platform.publish_engine_route("langgraph-2")
    again = await platform.engine_router.assign(event, route_version="langgraph-2")
    assert first.engine == again.engine == "legacy"
    assert await platform.active_delivery_paths(event) == 1
```

- [ ] **Step 2：运行测试确认失败。**

```bash
uv run pytest tests/integration/test_legacy_import.py tests/integration/test_engine_cutover.py tests/fault/test_version_retention.py -v
```

预期首次因尚未实现的导入、接口或目标行为失败；夹具自身报错先修复夹具，不能把无关失败当红灯。

- [ ] **Step 3：实现最小交付。**

按规格S1–S7检查现有ai-planner/ai-agents资产基线，preview产出team/Workspace字段映射、Skill资源/tool alias/脚本镜像转换、规则与强制策略区别、MCP绑定、通知渠道、历史事件/费用来源。manifest有source_id/version/content_hash/result，apply只导入已核对的具体manifest，不读取/导出真实密钥。新增migration_routes/import_records迁移表，幂等重跑不重复资产。历史SDK会话只作消息/摘要新Run来源，不生成伪LangGraph checkpoint，历史费用不混用新去重键。切流使用新入口EngineRouter作为两引擎共同的唯一接入/通知权威，source_event第一次分配的engine不可随Profile变更重选；旧新系统不能分别独立订阅同一来源后各自去重。legacy adapter只转发旧引擎，不声称新平台能力。将实际SeaTalk/告警回调配置变更列入手册，测试环境验证后才按授权切目标环境。在途旧任务由旧系统完成，新系统旧版本Run保留Worker/Skill/Codec/镜像；缺版本capability_blocked不替换。影子Run禁止所有外部写和通知（Tool层与Dispatcher双重检查），费用独立，回滚只改新受理。tests/support/migration.py（新增）定义source_event/publish_engine_route/active_delivery_paths并启动旧引擎测试桩模拟双方路由。

关键实现约束：

```python
def normalize_legacy_history(record: dict) -> dict:
    return {
        "kind": "imported_history",
        "source_id": record["source_id"],
        "source_engine": record["source_engine"],
        "messages": record["messages"],
        "resumable_checkpoint": False,
    }
```

- [ ] **Step 4：执行上述测试命令及已完成的相关契约，确认通过。**

通过条件：每项资产有来源hash和差异结果；重复来源跨切流只有一条主动执行/投递路径；旧Run恢复保持原版本。 保留实际执行输出到阶段验收记录。

- [ ] **Step 5：检查差异并提交当前任务。**

```bash
git diff --check
git add -- src/migration/legacy_assets.py src/migration/mapping.py src/migration/engine_router.py src/cli/import_legacy.py src/ingress/engine_routing.py src/persistence/migrations/versions/0003_legacy_routes.py tests/integration/test_legacy_import.py tests/integration/test_engine_cutover.py tests/fault/test_version_retention.py docs/runbooks/legacy-cutover.md docs/acceptance/legacy-mapping.md tests/support/migration.py
git diff --cached
git commit -m "feat: migrate legacy assets with version safe engine cutover"
```

仅包含本任务交付文件；某项文件未改动时从暂存命令去掉，不夹带其他项目内容。


### T24：容量、恢复演练、发布配置和 P0 完整验收

**依赖与依据：** T23；规格 章节 6、18、22、23.2/23.3、24；全部P0 AT。
**文件：** 创建或修改 `tests/capacity/test_sqlite_admission.py`、`tests/capacity/test_team_fairness.py`、`tests/fault/test_backup_restore.py`、`tests/fault/test_worker_recovery.py`、`tests/e2e/test_p0_acceptance.py`、`deploy/compose.local.yml`、`deploy/compose.production.yml`、`deploy/README.md`、`docs/runbooks/release.md`、`docs/runbooks/sqlite-recovery.md`、`docs/runbooks/action-reconciliation.md`、`docs/acceptance/M4.md`、`docs/acceptance/P0-matrix.md`、`tests/support/capacity.py`、`tests/contract/test_openapi_surface.py`。
**Interfaces：** 复用生产CLI dod db migrate/bootstrap/api/worker/scheduler/dispatcher/runner/doctor与备份恢复命令；AcceptanceReport列出每个AT的command、environment、versions、observed、status和evidence_ref。

- [ ] **Step 1：写失败测试及本任务列明的边界用例。**

```python
import pytest

@pytest.mark.asyncio
async def test_slow_sse_does_not_block_wal_checkpoint(capacity_environment):
    result = await capacity_environment.measure_slow_subscriber_with_writes()
    assert result.open_read_transactions_during_network_wait == 0
    assert result.wal_checkpoint_progressed is True
    assert result.run_admission_p95_ms <= 500
```

- [ ] **Step 2：运行测试确认失败。**

```bash
uv run pytest tests/unit tests/contract tests/integration tests/fault tests/e2e tests/capacity -m 'not live' -v
```

预期首次因尚未实现的导入、接口或目标行为失败；夹具自身报错先修复夹具，不能把无关失败当红灯。

- [ ] **Step 3：实现最小交付。**

tests/support/capacity.py（新增）实现capacity_environment，环境固定本地持久磁盘、1Worker/2Run、单团队2、无真实模型延迟的低竞争受理压测；记录硬件/磁盘/连接池/负载/样本量（至少1000次受理）、warmup、p50/p95/p99与STORAGE_BUSY/WAL/队列，不能把超时样本排除。另测两个团队轮转、恢复/回复优先、慢SSE不持读事务、30个受控长Run排队、模型/MCP超时预算有效。真实SIGKILL验证DB/工具可用前提下90秒内安排恢复；45s实际租约测试不能全部用缩短参数替代。备份在活跃WAL状态Online Backup，停写恢复后integrity/FK/ID/seq/父链/pending writes/对象sha和unknown动作事实对账；演练前禁止真实业务写，确认完成再开放Dispatcher。发布镜像采用 Node 构建阶段生成 web/dist/，运行阶段复制产物并验证 /app 深层刷新与静态资源，/v1 和 SSE 不被回退吞掉；发布执行兼容迁移→能力注册→Profile灰度→观测→保留旧版，生产ready拒development/临时SQLite。全进程同绝对URL，普通启动无DDL、15/30秒关闭实测，启动命令与OpenAPI同版。质量集必须人工确认30/10和95/80/80阈值、两类0漏检；缺live不算通过。报告逐项区分passed/failed/unavailable/deferred，AT-30/31仅M5 deferred。 test_openapi_surface按规格章节 11.2逐条比较method/path、响应码、认证、Idempotency-Key/If-Match/expected_revision及错误Schema，覆盖所有管理/Run/Ingress/上传/事件接口，不能只验证UI碰到的子集。

关键实现约束：

```python
def percentile_95(samples_ms: list[float]) -> float:
    import math
    if not samples_ms:
        raise ValueError("no measured samples")
    ordered = sorted(samples_ms)
    return ordered[math.ceil(len(ordered) * 0.95) - 1]
```

- [ ] **Step 4：执行上述测试命令及已完成的相关契约，确认通过。**

通过条件：另运行 uv run pytest tests/contract/live tests/e2e --run-live -m live -v，以及 uv run workbench evaluate --dataset tests/fixtures/business/manifest.json --output docs/acceptance/evaluation.json；全部P0 AT及质量/容量/备份门槛有真实证据才发布。 保留实际执行输出到阶段验收记录。

- [ ] **Step 5：检查差异并提交当前任务。**

```bash
git diff --check
git add -- tests/capacity/test_sqlite_admission.py tests/capacity/test_team_fairness.py tests/fault/test_backup_restore.py tests/fault/test_worker_recovery.py tests/e2e/test_p0_acceptance.py deploy/compose.local.yml deploy/compose.production.yml deploy/README.md docs/runbooks/release.md docs/runbooks/sqlite-recovery.md docs/runbooks/action-reconciliation.md docs/acceptance/M4.md docs/acceptance/P0-matrix.md tests/support/capacity.py tests/contract/test_openapi_surface.py
git diff --cached
git commit -m "test: qualify SQLite P0 capacity recovery and release gates"
```

仅包含本任务交付文件；某项文件未改动时从暂存命令去掉，不夹带其他项目内容。

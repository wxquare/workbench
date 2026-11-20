# Workbench 产品闭环 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 交付反馈→知识缺口→补写重放闭环、准确用量费用和最小统计页，并验收已有版本引用。

**Architecture:** 反馈与缺口在同一 UoW 完成追加和幂等；费用复用现有台账，不按 tokens 临时估算伪造账单。统计通过应用服务授权后调用 SQLite 聚合端口，页面复用现有 Vue/Element Plus 风格。

**Tech Stack:** FastAPI、SQLAlchemy/SQLite、Vue 3、TypeScript、Element Plus、pytest、Vitest；趋势图为 SVG，无图表依赖。

## Global Constraints

- 继承[总计划](2026-11-20-next-phase.md)约束；本包 23–36 Agent h，可与真实资料准备错峰推进。
- 用户反馈不改变原 RunOutcome，不伪装人工验证；知识补写发布与解决缺口仍需现有维护者流程。
- “未知计费不显示为零”；金额使用整数 amount_micros，按币种展示，pending 是独立状态。
- 统计接口只读，不用页面已加载的第一页做全量统计；当前权限与团队隔离优先于历史成员身份。

## Task C1：反馈追加与知识缺口事务（5–8 h）

**Files:**
- Modify `apps/agent/src/workbench_agent/api/feedback.py`、`evaluation/feedback.py`、`knowledge/gaps.py`、`persistence/backends/knowledge.py`。
- Modify `apps/agent/tests/integration/test_feedback_evaluation.py`、`test_kb_gaps.py`。
- Create `apps/agent/tests/integration/test_feedback_gap_transaction.py`。
- Modify `docs/06-reference/01-api.md`、`02-data-dictionary.md`（位于 docs/06-reference）、`docs/02-architecture.md`。

**Interfaces:** 保留 `POST /v1/runs/{run_id}/feedback` 的 rating/comment，增加可选 `reason: irrelevant|citation_error|stale|other`；负反馈缺 reason 归 other，正反馈不能携 reason。返回增加 `knowledge_gap_id: str|null`。新增 `KnowledgeGaps.record_feedback_in(uow, auth, run: dict, *, feedback_id: str) -> str|None`，只供已授权反馈事务调用，不伪造 ExecutionGuard，也不调用运行时 record()。

- [ ] **Step 1：先写真实事务断言。** 沿用 integration 的 client/platform 夹具；使用现有 KB 图夹具创建终态知识 Run。负反馈创建一条 feedback 和一条 low_quality gap；同 key 重放返回相同 feedback/gap；同 key 换内容409；不同 key 再点同一 Run 可追加反馈但仍一条 gap；模拟插 gap 失败时 feedback 和幂等回执一起回滚。非知识图不建 gap，跨团队404，connector403。
- [ ] **Step 2：红灯。** `cd apps/agent && uv run python -m pytest tests/integration/test_feedback_gap_transaction.py -q`。
- [ ] **Step 3：实现事务及去重。** FeedbackService.record 增加 keyword-only `reason=None`，保留原位置参数调用兼容。reason 存已有 JSON payload，不为它新增列；请求未传 reason 时维持旧幂等 payload 形状，避免部署升级后同 key 被误判冲突。记录反馈前验证当前 Run 访问权；只有终态 `knowledge.citation_review` 且存在有效 query_scope 的负反馈才生成 gap。
- [ ] **Step 4：同步缺口可见性。** `REASONS` 和 SQLiteKnowledgeStore.gaps 的 problem_type 过滤都增加 `low_quality`。去重 digest 固定 run_id+scope_id+reason=low_quality；已 resolved 的同一 gap 不因再次点踩自动重开，返回原 id。摘要复用 redacted_content，保存 source_trace 中反馈 id 和来源图；不把原始私密问题放进日志。gap 创建不要求已结束 Run 的 Worker lease；必须从受权 Run 的已存 scope 推导。
- [ ] **Step 5：绿灯与兼容。** 跑 feedback_evaluation、feedback_gap_transaction、kb_gaps；验证缺口→草稿→发布→replay→人工确认仍可用，新 gap 也能进入现有流程。新反馈不修改原 outcome/digest。建议 `feat: turn negative knowledge feedback into gaps`。

## Task C2：详情与聊天的反馈入口（3–5 h）

**Files:** Create `apps/web/src/feedback-api.ts`、`components/RunFeedback.vue`、`tests/feedback-api.test.ts`、`tests/run-feedback.test.ts`；Modify `views/RunDetail.vue`、`views/Workbench.vue`、`components/KnowledgeGaps.vue`（位于 apps/web/src）、`docs/01-product.md`。

**Interfaces:** `FeedbackApi.submit(runId: string, body: {rating: 1|-1; reason?: string; comment?: string}, key: string): Promise<FeedbackView>`；`RunFeedback` props 为 `runId: string, terminal: boolean`。服务器授权结果控制行为，不能只靠隐藏按钮。

- [ ] **Step 1：先写交互测试。** 终态回答显示有用/没用；负反馈选择原因后提交；双击不并发；POST 结果不确定重试沿用原 key；403/409失败保留输入且不显示成功；切 Run/token 时清空旧反馈和旧请求结果。
- [ ] **Step 2：红灯。** `cd apps/web && npm test -- tests/feedback-api.test.ts tests/run-feedback.test.ts`。
- [ ] **Step 3：实现共用组件和两处接线。** 负反馈原因显示答非所问、引用错误、资料过期、其他；评论可选。成功后展示已记录，返回 gap_id 且当前用户有知识管理权限时提供定向跳转；无缺口访问权仍可正常给反馈，不泄露 gap 正文。反馈在 Run 详情和 assistant 消息使用同一组件；两处不是两套状态或接口。
- [ ] **Step 4：绿灯和验收闭环。** 跑定向 Vitest 和 typecheck；用户操作一次“没用”后可在知识缺口页筛到同一 Run，转草稿、发布、重放、人工确认解决。不在 UI 中自动发布条目或自动标记解决。建议 `feat: add answer feedback controls`。

## Task C3：Run 用量卡和任务列表费用（4–6 h）

**Files:**
- Create `apps/web/src/usage.ts`、`components/RunUsage.vue`、`tests/usage.test.ts`、`tests/run-usage.test.ts`。
- Modify `apps/web/src/api.ts`、`views/RunDetail.vue`、`components/RunTable.vue`、`views/Runs.vue`。
- Modify `apps/agent/src/workbench_agent/application/inventory.py`、`persistence/ports/runs.py`、`persistence/backends/runs.py`，只在列表投影缺费用摘要时增加批量查询。
- Modify `apps/agent/tests/integration/test_run_inventory.py`、`test_usage_api.py`；同步 `docs/06-reference/01-api.md`、`docs/01-product.md`。

**Interfaces:** 详情复用 `GET /v1/usage?run_id=...` 的 UsageSummary；列表项增加 `usage_summary: {model_calls, input_tokens, output_tokens, amount_micros, currency, usage_pending, cost_pending, known_costs}`，原字段不删。`formatUsageCost(summary): string` 不使用 truthy 判断金额。

- [ ] **Step 1：用量展示反例测试。**

```typescript
import { expect, it } from 'vitest'
import { formatUsageCost } from '../src/usage'

it('keeps unknown cost distinct from confirmed zero', () => {
  expect(formatUsageCost({ amount_micros: null, currency: null, cost_pending: true, known_costs: {} })).toBe('计费待确认')
  expect(formatUsageCost({ amount_micros: 0, currency: 'USD', cost_pending: false, known_costs: { USD: 0 } })).toBe('USD 0.000000')
})
```

另测 USD/CNY 同时存在不相加、tokens未知、部分已知、微小费用不被显示成零、详情换 Run 后旧响应被丢弃。

- [ ] **Step 2：红灯。** `cd apps/web && npm test -- tests/usage.test.ts tests/run-usage.test.ts`；列表后端变更先补 test_run_inventory 的费用/跨团队测试再运行红灯。
- [ ] **Step 3：接详情和列表。** 详情显示模型调用次数、input/output/known tokens、按币种金额、待确认状态。完整总额仅当费用全部已确认且币种单一时显示；多币种按组展示；pending 时显示“已确认 USD …；其余计费待确认”。列表通过一页 Run ids 批量取台账摘要，不在前端为每行发一次 usage 请求，也不把 outcome 的过时金额当实时台账。
- [ ] **Step 4：绿灯和提交。** 检查列表数据查询次数不随行数线性增加；401/403/失败请求显示错误而非0；前端定向测试/typecheck、后端 usage_api/run_inventory 通过。建议 `feat: display run usage and pending billing`。

## Task C4：统计口径与授权聚合 API（7–10 h）

**Files:** Create `apps/agent/src/workbench_agent/statistics/service.py`、`persistence/ports/statistics.py`、`persistence/backends/statistics.py`、`api/statistics.py`、`tests/integration/test_statistics_api.py`；Modify `persistence/backends/sqlite.py`、`persistence/ports/backend.py`、`api/app.py`、`docs/01-product.md`、`06-reference/01-api.md`、`06-reference/02-data-dictionary.md`。

**Interfaces:** `GET /v1/stats/summary?start_at=ISO&end_at=ISO&agent_id=UUID`。范围半开 `[start,end)`，按 Run.created_at 选 cohort；≤90天，start/end 必须带时区，标准化 UTC；返回 `generated_at, cohort, definitions_version='stats-v1', counts, rates, failure_top5, latency, costs, gaps, feedback, trend`。趋势天桶按 Asia/Shanghai 明确返回 day。

应用层 `StatisticsService.summary(auth, *, start_at, end_at, agent_id=None) -> dict`；端口 `summary(uow, *, team_id, agent_ids: tuple[str,...], start_ms, end_ms) -> dict`。SQL 只能在 adapter 中。

先把以下默认口径写入产品文档，在实现前请用户复核。未收到改动时按本表编排实现任务；不将不同统计悄悄换分母：

| 指标 | 定义 |
| --- | --- |
| Run 总数 | 当前可访问 Agent 的 cohort 全状态 Run 数；另报在途数 |
| 终态数 | succeeded/partial/failed/cancelled/timed_out/expired |
| 成功数/率 | status=succeeded 且 outcome.completed=true；分母终态数 |
| 部分完成数/率 | status=partial；分母终态数；不并入成功 |
| 状态不一致 | succeeded 但 outcome缺失/不完整单列，不计成功 |
| 失败原因Top5 | status=failed/timed_out/expired，按持久化结构化错误码归类；未知为 unknown，不能拿原始异常文本作维度 |
| 平均耗时 | 终态且 started_at存在的 Run 的 active_elapsed_ms 平均；无样本为null；不使用 now-created_at替代 |
| 总成本 | cohort Run 已确认费用按币种分组；同时给 pending_run_count，pending 时总费用不完整 |
| 知识缺口 | cohort Run 关联的当前 open/resolved/dismissed 各自计数；不把 resolved 状态误当时间范围内解决事件 |
| 没用反馈率 | cohort 内每个 `(run_id,subject)` 的最新反馈中 rating=-1 占比；同毫秒按feedback_id稳定打破平局；无反馈为null |
| 日趋势 | 相同 cohort 和分母按上海自然日分桶；空日补0，率分母为0时null |

- [ ] **Step 1：先写带可手算数据的集成测试。** 同一 cohort 6条：2成功、1partial、1failed、1cancelled、1running，终态5→成功率0.4/部分率0.2；加入范围外、另一团队、已撤销 Agent 的 Run 不改变结果；对一个 subject 先赞后踩只记最新；两币种分开；无结果 null；起点包含终点排除。
- [ ] **Step 2：红灯。** `cd apps/agent && uv run python -m pytest tests/integration/test_statistics_api.py -q`。
- [ ] **Step 3：实现授权和聚合。** 统计页最低 maintainer，与知识缺口管理保持一致；拒绝 connector 和 agent_scope 非空受限身份。用现有 Authorization 当前权限逐 Agent 得到允许 id 集合，再传 adapter；agent_id 不可访问返回404。不得仅按 team_id 聚合而忽略当前 Agent 可见性。反馈、用量、缺口先各自按 Run/主体聚合再 join，避免一对多乘法放大费用/反馈数。
- [ ] **Step 4：实现结构化失败来源与复杂度验证。** 选择当前 runs/job 持久化的最后有效终态错误码，公开前映射成稳定业务类别；没有可靠错误码归 unknown。使用 EXPLAIN QUERY PLAN 记录范围查询，若实际缺索引再添加显式迁移及回滚测试，不预先强建无用索引；避免在服务层全量拉 Run 然后 Python 汇总。
- [ ] **Step 5：绿灯和提交。** 覆盖 viewer/operator 拒绝、maintainer允许、撤销权限、agent过滤、时间非法、90天上限、时间边界、零分母、多币种、join倍增；建议 `feat: aggregate authorized workbench statistics`。

## Task C5：最小统计页（3–5 h）

**Files:** Create `apps/web/src/statistics-api.ts`、`views/Statistics.vue`、`tests/statistics-api.test.ts`、`tests/statistics-view.test.ts`；Modify `router.ts`、导航对应的现有组件、`docs/01-product.md`。

**Interfaces:** 使用 C4 stats-v1；路由 `/statistics` 从 Placeholder 拆出，原 schedules/incidents 占位保留。筛选日期和可访问 Agent；展示 counts/rates/费用完整性与生成时间。

- [ ] **Step 1：先写交互测试。** 默认最近7个上海自然日，允许选择≤90天；Agent切换重载、旧请求响应丢弃；403/500不显示伪零；空样本率为“—”；定义说明列出分母；费用 pending 不展示完整总额。
- [ ] **Step 2：红灯。** `cd apps/web && npm test -- tests/statistics-api.test.ts tests/statistics-view.test.ts`。
- [ ] **Step 3：实现页面和 SVG。** 展示 Run总数/成功率/部分完成率、Top5、平均耗时、分币种费用、缺口、负反馈率；SVG按日显示 Run/成功/部分数量，提供可读图例和文本表格。不引入其他筛选维度、图表依赖或导出平台。
- [ ] **Step 4：绿灯和人工验收。** 定向Vitest、typecheck/build；用 C4 可手算样本核对数字和时间边界；桌面与窄屏检查加载/错误/空数据/多币种。建议 `feat: add minimal statistics page`。

## Task C6：已有版本引用回归与必要修补（1–2 h）

**Files:** Read/仅必要时 Modify `apps/web/src/knowledge-citations.ts`、`components/KnowledgeAnswer.vue`、`views/KnowledgeEntry.vue`、`tests/knowledge-citations.test.ts`；Create `tests/knowledge-version-journey.test.ts`。

**Interfaces:** 保留已有 `/knowledge/:id/versions/:version#chunk_id` 与 verifiedCitations；不更换引用协议，不新增第二套原文查看器。

- [ ] **Step 1：先跑已有检查。** `cd apps/web && npm test -- tests/knowledge-citations.test.ts tests/knowledge-view.test.ts`；已有组件已含精确版本跳转、hash滚动和 :target高亮，不能按缺失功能重写。
- [ ] **Step 2：补缺少的用户路径断言。** 用旧版本引用，发布新版本后点击仍读旧版/正确chunk；同页换hash滚动；撤销当前访问权后不展示缓存原文；quote不属于证据则不渲染回答；无此chunk不高亮其他段落。
- [ ] **Step 3：只修测试揭示的问题。** 若新测试因具体行为缺失而红，最小修复；已有行为通过则只交回归证据，不制造重构。运行定向测试和typecheck。
- [ ] **Step 4：交付。** 建议 `test: verify pinned knowledge citation navigation`；实际只加测试时按此标题，不把它记成新实现的整项功能。

## 本包验收

- [ ] 点一次“没用”能够关联可处理缺口，重复点击/重试不造成重复缺口。
- [ ] 费用真实零、未知、部分已知、多币种均有正确表达，列表无逐行请求。
- [ ] 统计分母、时间、当前权限和数据完整性符合 stats-v1。
- [ ] 引用固定版本，旧内容不随新发布漂移，当前权限变化生效。

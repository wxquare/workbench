# Workbench 工具管理 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** admin 可查看已授权 MCP 连接和工具，保存/探测/发布只读绑定，并明确发布到 Agent 配置后生效。

**Architecture:** 部署配置仍是连接和授权的唯一来源；管理服务只投影当前 Agent 获授的连接。发现通过现有 AdminJob/Worker 模式执行，绑定保持不可变版本，Profile 显式引用其 VersionRef。

**Tech Stack:** FastAPI、现有 AdminJob、MCPAdapter、Vue/TypeScript/Element Plus、pytest/Vitest；不新增连接密钥管理。

## Global Constraints

- 继承[总计划](2026-11-20-next-phase.md)。本包 16–24 Agent h，可在 A 真实资料等待时推进。
- 用户已确认工具管理保留 **admin**；不将 maintainer 提升为工具发布者。
- 用户已确认 D2 首批仅 knowledge_search；管理页可展示部署允许的其他只读工具，但标识本期工具对话兼容清单，不能暗示新图已支持所有工具。
- 不允许通过页面输入任意 endpoint、Authorization header、secret 或放宽部署授权。
- MCP readOnlyHint 不是授权凭据；只读允许来自部署 `read_tools`，绑定自身也须 read_only=true。

## 1 HTTP 与页面契约

新增接口均在 `agent_id` 下，先做当前团队/Agent admin 授权：

| 接口 | 返回/语义 |
| --- | --- |
| GET `/v1/agents/{agent_id}/tool-connections` | `items[{server,credential_scope,read_tools,status}]`；仅该 team+agent 授权连接，不含 endpoint/secret_ref |
| POST `/v1/agents/{agent_id}/tool-connections/{server}/discover` | body=`{credential_scope}`；Idempotency-Key；202 AdminJob，kind=tool_discovery |
| GET `/v1/agents/{agent_id}/admin-jobs/{job_id}` | 复用现有接口，tool_discovery 的 result_ref 为经过裁剪的 JSON 结果 |
| GET `/v1/agents/{agent_id}/tool-bindings` | 分页 `{items,next_cursor}`；每 alias 的 draft revision、最新已发布 VersionRef 和当前 Profile 引用版本 |
| GET `/v1/agents/{agent_id}/tool-bindings/{alias}` | 草稿、revision、版本列表摘要；无草稿返回 draft=null，不虚构默认已保存草稿 |
| GET `/v1/agents/{agent_id}/tool-bindings/{alias}/versions/{version}` | 精确版本 binding + VersionRef；权限仍按当前成员校验 |
| PUT/POST 现有草稿、publish、probe | 保留原路由、If-Match 和 Idempotency-Key 语义 |

POST 发现只做 metadata list_tools，不触发业务调用。既有 probe 可能按部署 probe_requests 实际只读调用，UI 必须分别显示 configuration/discovery/schema/invocation；invocation=not_run 时不能显示“真实调用通过”。

## Task D1.1：连接发现和绑定读接口（6–9 h）

**Files:**
- Create `apps/agent/src/workbench_agent/api/tool_connections.py`、`tools/management.py`、`tools/discovery.py`。
- Modify `api/tool_bindings.py`、`api/app.py`、`domain/admin_jobs.py`、`cli/worker.py`、`persistence/ports/tools.py`、`persistence/backends/tools.py`。
- Create `apps/agent/tests/integration/test_tool_management_api.py`、`tests/contract/test_tool_discovery.py`。
- Modify `docs/06-reference/01-api.md`、`03-configuration.md`、`docs/02-architecture.md`、`docs/05-decisions.md`。

**Interfaces:** `ToolManagement.connections(auth, agent_id) -> dict`；`enqueue_discovery(auth, agent_id, *, server, credential_scope, key) -> AdminJob`；`list_bindings(auth, agent_id, *, limit, after) -> dict`；`get_binding(auth, agent_id, alias) -> dict`；`get_version(auth, agent_id, alias, version) -> dict`；`ToolDiscovery.execute(claim) -> str`，结果 JSON 含 connection/status/tools。

- [ ] **Step 1：测试权限与脱敏。** admin 只能发现自身 Agent 授权连接；其他 Agent/team404，maintainer403，connector/受限身份拒绝。假 secret、raw endpoint、secret_ref 均不出现在 list/job result/错误响应；list_tools 返回未获授权名称时被过滤，重复工具名、游标循环、超大描述、超时返回结构化失败。
- [ ] **Step 2：红灯。** `cd apps/agent && uv run python -m pytest tests/integration/test_tool_management_api.py tests/contract/test_tool_discovery.py -q`。
- [ ] **Step 3：实现管理服务与端口。** 查 `settings.service.mcp_connections` 时只匹配 `(auth.team_id, agent_id)`；server+credential_scope 必须唯一命中配置，无法从 body 注入其他地址。绑定读取通过 Store/UoW 且按 team/agent/alias/version 过滤，稳定分页 limit≤100。
- [ ] **Step 4：新增 tool_discovery AdminJob。** `ADMIN_JOB_KINDS` 和 Worker registry 同步注册；执行前/结果提交前按既有 ToolProbe 模式校验 AdminJobGuard 与当前 admin 权限。调用 MCPAdapter.discover，超时10秒；输出工具仅保留名称、截断描述、schema、readOnlyHint、部署只读允许状态、模型schema兼容性。schema/描述总输出上限256KiB，不记录模型或 MCP 凭据；不允许 API handler 直接保管 MCP 会话。
- [ ] **Step 5：绿灯与文档。** 测发现过程中撤销权限、连接移除、工作进程缺 capability；覆盖旧 tool_probe/admin-job 不回归。建议 `feat: expose authorized tool management inventory`。

## Task D1.2：发布边界、模型 schema 投影及 Profile 校验（3–5 h）

**Files:** Create `apps/agent/src/workbench_agent/tools/model_schema.py`、`tests/unit/test_model_tool_schema.py`；Modify `tools/registry.py`、`runtime/experts/knowledge.py`、`application/profiles.py`、`tests/integration/test_graph_profile_validation.py`、`tests/contract/test_tool_policy.py`。

**Interfaces:** `project_model_schema(tool_name: str, published_schema: dict) -> dict`；`validate_model_schema(schema: dict) -> None`；knowledge.py 的旧 `model_tool_schema` 和 `_schema_model` 入口兼容保留。工具对话能力要求最多4个**绑定**，默认4次**调用**，两者是不同限制；首批只允许 knowledge_search 绑定。

- [ ] **Step 1：先写兼容反例。** 完整knowledge_search MCP schema含scope/context，合法投影只有query/scene/limit，应允许；未知模型参数出现嵌套object/array/$ref时拒绝；闭合对象的字符串/整数/数字/布尔标量可用；新HTTP管理入口read_only=false拒绝；已有Action内部提案路径及旧历史版本读取仍通过。
- [ ] **Step 2：红灯。** `cd apps/agent && uv run python -m pytest tests/unit/test_model_tool_schema.py tests/integration/test_graph_profile_validation.py -q`。
- [ ] **Step 3：提取现有模型 schema 投影。** 不更改完整 published_schema 的内容/hash；将知识专家现有安全投影逻辑移至共用模块并保留旧入口转调。完整 schema 仍用现有 validate_tool_schema；模型字段再检查闭合对象和标量子集。D1 页面发布只读绑定时给兼容错误码和具体字段路径；不能拿“完整schema嵌套”误报现有知识工具不可接入。
- [ ] **Step 4：发布与生效检查。** HTTP管理入口保存/发布时在服务端调用新的 `validate_managed_binding(auth, agent_id, binding)`，拒绝read_only=false，并校验部署允许的tool_name/server/credential_scope；不能只靠UI。ToolRegistry的内部publish不全局禁用write绑定，因为现有Action提案链及其测试使用该入口创建待审批绑定；原有Action提案→审批→Outbox保留，新管理入口没有写工具选项。工具对话兼容性限制在目标Profile发布时再次验证；Profile.validate_refs对conversation.tool_use（D2注册后）检查最多4、alias唯一、全只读、首批仅knowledge_search，版本/hash/团队归属使用现有验证。
- [ ] **Step 5：绿灯和提交。** 对旧知识/RCA/SOP的schema/权限契约做对照，并跑 `tests/contract/test_tool_policy.py`、`tests/integration/test_action_contract.py`、`tests/e2e/test_synthetic_proposal_journey.py`，确认写绑定仍只生成提案、不直接调用transport。新管理入口发布失败不插入版本；D2未注册时不提前让未知图受理。建议 `feat: validate read-only tool publication and compatibility`。

## Task D1.3：工具页、绑定编辑和 Profile 生效（7–10 h）

**Files:** Create `apps/web/src/tools-api.ts`、`views/Tools.vue`、`components/AgentTools.vue`、`tests/tools-api.test.ts`、`tests/tools-view.test.ts`、`tests/agent-tools.test.ts`；Modify `views/Agents.vue`、`agents-api.ts`、`router.ts`、对应导航；同步 `docs/01-product.md`、`03-development.md`。

**Interfaces:** 工具页 `/tools` 替换占位；先选 Agent，获取 D1.1 数据。`AgentTools` 接收 Agent id、Profile draft、可管理标志，emit `update:tools(VersionRef[])`；不得直接原地修改其他 Profile 字段或自动发布 Profile。

- [ ] **Step 1：先写用户行为测试。** agent选择→发现提交202→轮询job→呈现工具；保存 If-Match 冲突保留输入；publish 返回 VersionRef 后仅更新草稿引用；当前已生效Profile仍显示旧版本，直到用户明确发布配置；不显示secret或编辑endpoint入口。
- [ ] **Step 2：红灯。** `cd apps/web && npm test -- tests/tools-api.test.ts tests/tools-view.test.ts tests/agent-tools.test.ts`。
- [ ] **Step 3：实现分步操作。** 发现列表按部署 read_tools 可选；生成绑定草稿时 alias/permission/timeout/max_output_bytes 显示可见默认值（10s/65536B），完整 schema 只读查看；保存、探测、发布分开按钮及状态。版本、草稿revision、Profile引用展示清楚；probe的schema不匹配必须阻止当前UI流程继续发布并提示重新获取schema，服务端仍自行校验。
- [ ] **Step 4：接入 Agent 配置工具区。** admin 可选已发布版本→加入 tools→保存 Profile→使用现有配置发布操作生效。maintainer可按页面当前权限看不可操作提示，不通过后端admin接口读取受限配置。发现/探测执行完后权限被撤销应显示403并清旧数据；切 Agent/token 取消旧轮询与延迟响应。
- [ ] **Step 5：绿灯和验收。** 定向 Vitest + typecheck/build；后端 D1.1–D1.2 定向测试。人工验收一条knowledge_search从发现到发布、Profile生效、再读精确版本；业务工具标“本期工具对话暂不启用”。建议 `feat: add agent tool management pages`。

## 本包验收

- [ ] 部署配置仍为连接授权来源；工具列表仅当前Agent可见且无密钥/地址泄露。
- [ ] admin完成读取→保存→探测→发布→绑定→发布配置；不把绑定发布误当Profile已生效。
- [ ] schema的完整授权参数和模型输入投影区分；复杂模型参数在发布/选择时明确拒绝。
- [ ] 不新增写工具通道，已有图与不可变版本保持兼容。

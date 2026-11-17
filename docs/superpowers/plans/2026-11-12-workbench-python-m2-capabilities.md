---
title: "DoD Agents Python：M2 能力执行实施计划"
date: "2026-11-12"
status: ready-for-implementation
---

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让统一持久化运行时真正执行 DeepSeek、知识、工具、Skill、仓库脚本和受约束专家。
**Architecture:** 统一ModelInvoker与PolicyToolExecutor检查权限/预算/台账，知识与代码结果进入Evidence；Runner隔离文件和脚本执行。
**Tech Stack:** LangChain 1.x、LangGraph 1.x、langchain-deepseek、MCP、S3/MinIO、Docker、SQLite。

## Global Constraints

遵守[总计划](2026-11-12-workbench-python-implementation-plan.md)全部约束。DeepSeek版本固定、SDK重试0/Invoker最多3总尝试、JSON最多1次修正、预算含专家/评估、真实连接验收不得替代为mock；秘密仅按引用注入。

## 实施任务

### T10：父 Run 预算预留、结算和重试用量

**依赖与依据：** T09；规格 章节 10.4、15.2/15.4、19.3；AT-07/19/26。
**文件：** 创建或修改 `src/domain/usage.py`、`src/runtime/budgets/service.py`、`src/runtime/budgets/limits.py`、`src/persistence/usage.py`、`tests/support/budgets.py`、`tests/integration/test_expert_budget.py`、`tests/integration/test_usage.py`。
**Interfaces：** BudgetService.reserve(guard, request: ReservationRequest) -> Reservation；settle(guard, invocation_id, usage: ActualUsage) -> UsageRecord；snapshot(auth, run_id) -> UsageSummary。ReservationRequest 含 invocation_id、kind、model_attempts、tool_attempts、token_upper_bound。

- [ ] **Step 1：写失败测试及本任务列明的边界用例。**

```python
import asyncio
import pytest

@pytest.mark.asyncio
async def test_concurrent_experts_cannot_overspend_parent(platform):
    run = await platform.create_run(model_call_limit=1)
    guard = await platform.claim(run.run_id)
    results = await asyncio.gather(
        platform.reserve_usage(guard, invocation_id="expert-a", model_attempts=1),
        platform.reserve_usage(guard, invocation_id="expert-b", model_attempts=1),
        return_exceptions=True,
    )
    assert sum(platform.is_reserved(result) for result in results) == 1
    summary = await platform.budgets.snapshot(platform.auth, run.run_id)
    assert summary.reserved_model_attempts == 1
```

- [ ] **Step 2：运行测试确认失败。**

```bash
uv run pytest tests/integration/test_expert_budget.py tests/integration/test_usage.py tests/fault/test_sqlite_contention.py -v
```

预期首次因尚未实现的导入、接口或目标行为失败；夹具自身报错先修复夹具，不能把无关失败当红灯。

- [ ] **Step 3：实现最小交付。**

实现规格四组默认预算、调用前数据库原子预留、调用后幂等结算，预留/已耗/待核对分开。每次真实网络尝试都扣尝试次数；成功回执复用不再扣。Token 包含输入/输出、专家和评估，max_context独立校验；为 compose_outcome 保留一次汇总预算，无额度改确定性 partial 模板。未获 usage 的请求保持 usage_pending，已发送请求不当未发生退款；价格缺失 cost_pending、整数微单位。Guard保护结算，失效尝试只写隔离审计，再由合法协调路径补账。tests/support/budgets.py 增加 reserve_usage/is_reserved；扩展 create_run 的客户端预算下调参数。验证并发预留、重复结算、Worker崩溃待决预留、恢复不刷新额度、active时间只算执行区间及evaluation子任务费用。

关键实现约束：

```python
def has_capacity(limit: int, settled: int, reserved: int,
                 requested: int, summary_reserve: int = 0) -> bool:
    return settled + reserved + requested + summary_reserve <= limit
```

- [ ] **Step 4：执行上述测试命令及已完成的相关契约，确认通过。**

通过条件：并行专家与重试不超发父额度；未知费用不显示为0；恢复前后预算守恒。 保留实际执行输出到阶段验收记录。

- [ ] **Step 5：检查差异并提交当前任务。**

```bash
git diff --check
git add -- src/domain/usage.py src/runtime/budgets/service.py src/runtime/budgets/limits.py src/persistence/usage.py tests/support/budgets.py tests/integration/test_expert_budget.py tests/integration/test_usage.py
git diff --cached
git commit -m "feat: enforce shared run budgets and usage settlement"
```

仅包含本任务交付文件；某项文件未改动时从暂存命令去掉，不夹带其他项目内容。


### T11：DeepSeek 配置管理、统一模型调用与真实能力探针

**依赖与依据：** T10；规格 章节 10.3、11.2、18.1；AT-33–36。
**文件：** 创建或修改 `src/runtime/models/profiles.py`、`src/runtime/models/factory.py`、`src/runtime/models/invoker.py`、`src/runtime/models/deepseek.py`、`src/runtime/models/json_output.py`、`src/api/model_profiles.py`、`src/application/model_profiles.py`、`src/connectors/secrets.py`、`tests/support/models.py`、`tests/integration/test_model_bootstrap.py`、`tests/integration/test_model_binding.py`、`tests/contract/test_model_errors.py`、`tests/contract/live/test_deepseek.py`、`docs/acceptance/deepseek.md`。
**Interfaces：** ModelFactory.build(profile: ModelProfileVersion, secrets: SecretProvider) -> BaseChatModel；ModelInvoker.invoke(guard, role, messages, output_schema=None) -> ModelResult；ModelProbe.run(auth, profile_id) -> ProbeReport。输出含 requested/response model、usage、cost_state、validated_data。

- [ ] **Step 1：写失败测试及本任务列明的边界用例。**

```python
import pytest

@pytest.mark.asyncio
async def test_blank_json_gets_only_one_budgeted_repair(platform):
    run = await platform.create_run()
    guard = await platform.claim(run.run_id)
    platform.models.script_outputs(["   ", '{"summary": 7}'])
    result = await platform.model_invoker.invoke(
        guard, role="summary", messages=platform.summary_messages(),
        output_schema=platform.run_outcome_schema,
    )
    assert result.status == "invalid_output"
    assert platform.models.attempt_count == 2
    assert await platform.persisted_model_attempts(run.run_id) == 2
```

- [ ] **Step 2：运行测试确认失败。**

```bash
uv run pytest tests/integration/test_model_bootstrap.py tests/integration/test_model_binding.py tests/contract/test_model_errors.py -v
```

预期首次因尚未实现的导入、接口或目标行为失败；夹具自身报错先修复夹具，不能把无关失败当红灯。

- [ ] **Step 3：实现最小交付。**

实现模型 draft/PATCH/publish/probe接口和字段校验，SecretProvider仅Worker解析真实值。集中通过 langchain-deepseek ChatDeepSeek 构建：固定model/base_url/参数，SDK max_retries=0，provider_options允许列表只映射thinking.type=disabled等已批准字段；具体构造参数以锁定依赖API文档和契约测试为准。ModelInvoker统一截止/预算/Trace/attempt，429遵循Retry-After，5xx/timeout最多3总尝试；401/403/缺secret/无效model/空输出/Schema分别错误，不切模型。最后JSON调用与工具循环分离，显式JSON提示及示例，一次修正也受总预算。缺secret创建Run MODEL_PROFILE_UNAVAILABLE，运行中MODEL_CREDENTIAL_UNAVAILABLE。tests/support/models.py定义 scripted模型输出、summary_messages、run_outcome_schema、persisted_model_attempts；采用严格str模型字段避免Pydantic隐式转换。真实probe验证文本、至少一轮实际工具调用、流式增量、JSON、usage和取消；只用无业务写入测试工具。记录model别名实际响应元数据，不宣称权重可复现。重跑bootstrap/排队后换绑定/恢复/角色继承/日志脱敏。 probe使用T02的AdminJobService持久化；Profile与工具健康分别标配置/发现/调用，HTTP只返回管理Job受理。

关键实现约束：

```python
import json
from pydantic import BaseModel

def validate_json_output(text: str, schema: type[BaseModel],
                         finish_reason: str | None) -> BaseModel:
    if not text.strip() or finish_reason == "length":
        raise ValueError("empty or truncated structured output")
    payload = json.loads(text)
    return schema.model_validate(payload)
```

- [ ] **Step 4：执行上述测试命令及已完成的相关契约，确认通过。**

通过条件：离线错误与版本测试通过；另运行 uv run pytest tests/contract/live/test_deepseek.py --run-live -v，真实六类能力全部有版本化记录。 保留实际执行输出到阶段验收记录。

- [ ] **Step 5：检查差异并提交当前任务。**

```bash
git diff --check
git add -- src/runtime/models/profiles.py src/runtime/models/factory.py src/runtime/models/invoker.py src/runtime/models/deepseek.py src/runtime/models/json_output.py src/api/model_profiles.py src/application/model_profiles.py src/connectors/secrets.py tests/support/models.py tests/integration/test_model_bootstrap.py tests/integration/test_model_binding.py tests/contract/test_model_errors.py tests/contract/live/test_deepseek.py docs/acceptance/deepseek.md
git diff --cached
git commit -m "feat: integrate versioned DeepSeek model invocation and probes"
```

仅包含本任务交付文件；某项文件未改动时从暂存命令去掉，不夹带其他项目内容。


### T12：工具策略层、MCP 与读取型业务连接器

**依赖与依据：** T11；规格 章节 9.2、10.1、16.3、17；AT-03/07/36。
**文件：** 创建或修改 `src/domain/tools.py`、`src/domain/evidence.py`、`src/tools/policy.py`、`src/tools/mcp.py`、`src/tools/registry.py`、`src/connectors/knowledge.py`、`src/connectors/monitoring.py`、`src/connectors/logs.py`、`src/connectors/traces.py`、`src/connectors/dod.py`、`src/api/tool_bindings.py`、`tests/support/tools.py`、`tests/contract/test_tool_policy.py`、`tests/contract/test_mcp.py`、`tests/contract/live/test_read_connectors.py`、`tests/fixtures/business/manifest.json`。
**Interfaces：** PolicyToolExecutor.execute(context: RunContext, guard, invocation_id, binding_ref, args: dict) -> ToolResult；MCPAdapter.discover(connection_ref, credential_scope) -> ToolCatalog；Knowledge/Monitoring/Logs/Trace/DutyConnector.query(request, context) -> ConnectorResult。工具身份参数不接受模型覆盖。

- [ ] **Step 1：写失败测试及本任务列明的边界用例。**

```python
import pytest

@pytest.mark.asyncio
async def test_read_timeout_cannot_be_reported_as_normal(platform):
    run = await platform.create_run()
    guard = await platform.claim(run.run_id)
    platform.tool_transport.raise_timeout_for("logs.search")
    result = await platform.execute_tool(guard, alias="logs.search",
                                         args={"service": "payment-api"})
    assert result.status == "timeout"
    assert result.data is None
    assert result.error.retryable is True
```

- [ ] **Step 2：运行测试确认失败。**

```bash
uv run pytest tests/contract/test_tool_policy.py tests/contract/test_mcp.py tests/integration/test_tenant_isolation.py -v
```

预期首次因尚未实现的导入、接口或目标行为失败；夹具自身报错先修复夹具，不能把无关失败当红灯。

- [ ] **Step 3：实现最小交付。**

为binding alias/provider/版本/Schema/secret_ref/scope/read_only/timeout/retry_policy做草稿管理和分层probe。调用前持久化 logical invocation与参数摘要，检查用户∩团队∩Workspace∩Skill∩专家权限、即时撤权、输入Schema、预算，再在事务外调用，输出Schema/大小截断和Evidence登记。MCP按server+credential_scope独立连接/headers，取消和发现健康与实际调用分开。读取最多3总尝试退避0.5/2秒，Policy/图不叠加；denied/invalid_input不重试；写型工具只提议Action，T19执行。测试Schema变更、同alias版本、用户Header隔离、timeout/empty/unavailable区分、prompt injection不能提权。实际复用ai-planner MCP服务：knowledge_retrieve_v2/common_monitor_query/sre_log_trace_query/common_dod等能力映射先discover核对真实名称，不能把Go文件名直接当MCP tool name。初始化30案例manifest仅记录输入/来源/待人工审核状态，不捏造标签。

关键实现约束：

```python
from enum import StrEnum

class ToolStatus(StrEnum):
    OK = "ok"
    EMPTY = "empty"
    UNAVAILABLE = "unavailable"
    DENIED = "denied"
    INVALID_INPUT = "invalid_input"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"

def effective_permissions(user, team, workspace, skill, expert):
    return set(user) & set(team) & set(workspace) & set(skill) & set(expert)
```

- [ ] **Step 4：执行上述测试命令及已完成的相关契约，确认通过。**

通过条件：权限与错误分类均由代码约束；uv run pytest tests/contract/live/test_read_connectors.py --run-live -v 为知识/监控/日志/Trace/DoD分别留下实际调用记录。 保留实际执行输出到阶段验收记录。

- [ ] **Step 5：检查差异并提交当前任务。**

```bash
git diff --check
git add -- src/domain/tools.py src/domain/evidence.py src/tools/policy.py src/tools/mcp.py src/tools/registry.py src/connectors/knowledge.py src/connectors/monitoring.py src/connectors/logs.py src/connectors/traces.py src/connectors/dod.py src/api/tool_bindings.py tests/support/tools.py tests/contract/test_tool_policy.py tests/contract/test_mcp.py tests/contract/live/test_read_connectors.py tests/fixtures/business/manifest.json
git diff --cached
git commit -m "feat: add policy enforced MCP and evidence connectors"
```

仅包含本任务交付文件；某项文件未改动时从暂存命令去掉，不夹带其他项目内容。


### T13：Skill 导入发布、按需加载与知识引用

**依赖与依据：** T12；规格 章节 7.1、9.1/9.2、21.1；AT-04/08/22。
**文件：** 创建或修改 `src/skill_management/importer.py`、`src/skill_management/validator.py`、`src/skill_management/loader.py`、`src/skill_management/versions.py`、`src/workspaces/service.py`、`src/api/skills.py`、`src/runtime/graphs/knowledge.py`、`tests/support/skills.py`、`tests/contract/live/test_knowledge.py`、`tests/integration/test_skill_versions.py`、`tests/fault/test_version_retention.py`、`docs/runbooks/skill-import.md`。
**Interfaces：** SkillImporter.import_bundle(auth, artifact_ref | git_ref) -> ImportJob；SkillService.publish(auth, skill_id, version, revision) -> SkillVersion；SkillLoader.load(context, skill_ref, resource_path) -> VerifiedResource；KnowledgeService.retrieve(context, query) -> RetrievalResult。 VerifiedResource含content/content_hash/skill_version_ref；content_hash为单资源摘要，不与整个Skill包哈希混用。

- [ ] **Step 1：写失败测试及本任务列明的边界用例。**

```python
import pytest

@pytest.mark.asyncio
async def test_run_keeps_selected_skill_after_workspace_rollout(platform):
    skill_v1 = await platform.publish_skill("payments", body="v1")
    run = await platform.create_run()
    await platform.publish_skill("payments", body="v2")
    await platform.bind_latest_skill("payments")
    context = await platform.context_for(run.run_id)
    resource = await platform.skills.load(context, skill_v1.ref, "SKILL.md")
    assert resource.content == "v1"
    assert resource.skill_version_ref == skill_v1.ref
```

- [ ] **Step 2：运行测试确认失败。**

```bash
uv run pytest tests/integration/test_skill_versions.py tests/fault/test_version_retention.py tests/contract/live/test_knowledge.py --run-live -v
```

预期首次因尚未实现的导入、接口或目标行为失败；夹具自身报错先修复夹具，不能把无关失败当红灯。

- [ ] **Step 3：实现最小交付。**

实际 Skill 包从根目录 skills/<skill-name>/ 导入，Python 管理代码仅放 src/skill_management/；发布资源仍复制到不可变对象存储并记录版本/摘要，不能直接使用可变源码目录作为恢复依据。部署按 WORKBENCH_ASSET_ROOT 查找种子资源。导入/验证通过持久化Job，不在HTTP BackgroundTasks；校验压缩包路径、链接越界、哈希、文件大小、SKILL.md、tool alias/脚本/镜像依赖。适配.claude工具名/Hook/变量/权限清单，不能把同名文件当等价执行语义。发布不可变，回滚更新Workspace绑定；有Run引用禁止清理，缺版本capability_blocked不换新版本。加载目录摘要→选中SKILL.md→需要的资源，保留来源hash。知识检索记录query/数据源/hit/status/reference，真实工具失败不生成未命中知识缺口；外部内容不能修改策略。tests/support/skills.py提供publish_skill/bind_latest_skill/context_for；用真实知识源检查引用可访问、团队授权、过期/冲突/空结果/不可用分开。Skill和Model管理接口均复用revision/幂等/审计。 SkillImporter返回AdminJob的job_id与原始受理响应，bootstrap与import不复用Run id。

关键实现约束：

```python
from pathlib import PurePosixPath

def validate_bundle_path(path: str) -> str:
    candidate = PurePosixPath(path)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("invalid skill resource path")
    return candidate.as_posix()
```

- [ ] **Step 4：执行上述测试命令及已完成的相关契约，确认通过。**

通过条件：Skill版本固定、资源摘要与权限有效；真实检索产生可访问引用，故障不伪装无命中。 保留实际执行输出到阶段验收记录。

- [ ] **Step 5：检查差异并提交当前任务。**

```bash
git diff --check
git add -- src/skill_management/importer.py src/skill_management/validator.py src/skill_management/loader.py src/skill_management/versions.py src/workspaces/service.py src/api/skills.py src/runtime/graphs/knowledge.py tests/support/skills.py tests/contract/live/test_knowledge.py tests/integration/test_skill_versions.py tests/fault/test_version_retention.py docs/runbooks/skill-import.md
git diff --cached
git commit -m "feat: version skills and load traceable knowledge resources"
```

仅包含本任务交付文件；某项文件未改动时从暂存命令去掉，不夹带其他项目内容。


### T14：对象产物、固定仓库快照与受控 Runner

**依赖与依据：** T13；规格 章节 8.4、9.3/9.4、11.2；AT-08/09。
**文件：** 创建或修改 `src/persistence/objects/s3.py`、`src/workspaces/repositories.py`、`src/workspaces/snapshots.py`、`src/tools/workspace.py`、`src/runner/client.py`、`src/runner/service.py`、`src/cli/runner.py`、`src/api/uploads.py`、`runner/Dockerfile`、`runner/protocol.schema.json`、`tests/support/runner.py`、`tests/contract/test_runner_isolation.py`、`tests/integration/test_artifacts.py`、`tests/e2e/test_skill_runner.py`。
**Interfaces：** RepositoryService.sync(auth, workspace_id, repo_id, ref) -> SnapshotJob；WorkspaceExecutor.execute(context, snapshot_ref, image_digest, argv: list[str], limits) -> RunnerResult；ArtifactService.begin_upload/complete(auth, upload_id, sha256) -> Artifact。RunnerResult含exit_code/stdout/stderr摘要、Artifacts、timeout/cancel状态。

- [ ] **Step 1：写失败测试及本任务列明的边界用例。**

```python
import pytest

@pytest.mark.asyncio
async def test_symlink_escape_is_rejected(runner_workspace):
    outside = runner_workspace.parent / "secret.txt"
    outside.write_text("restricted")
    (runner_workspace / "escape").symlink_to(outside)
    with pytest.raises(ValueError, match="outside workspace"):
        runner_workspace.executor.resolve_path("escape")
```

- [ ] **Step 2：运行测试确认失败。**

```bash
uv run pytest tests/contract/test_runner_isolation.py tests/integration/test_artifacts.py tests/e2e/test_skill_runner.py -v
```

预期首次因尚未实现的导入、接口或目标行为失败；夹具自身报错先修复夹具，不能把无关失败当红灯。

- [ ] **Step 3：实现最小交付。**

Git sync准备新commit快照，Run冻结SHA，运行目录不git pull；容器只读代码镜像+任务目录，脚本来自发布Skill/授权资源。生产路径校验使用容器挂载边界与受控文件句柄避免symlink换位TOCTOU，示例仅表达根范围规则。执行结构化argv不用shell=True，限制2vCPU/2GiB/60s/20MiB，digest固定、独立进程组、取消/超时kill整个组；按进程/容器注入凭据不修改共享os.environ。Runner协议与调用鉴权、心跳、镜像/容量ready在此交付；Runner纯文件/脚本任务走同一Job/台账。S3完成上传先验摘要再available，失败留uploading不可被模型/恢复引用，授权短时下载。tests/support/runner.py定义runner_workspace及executor，另用真实容器验证fork子进程取消、网络策略、超限、失败上传、同Run固定commit和跨Worker读对象。compose添加MinIO和Runner镜像映射；uv run workbench runner命令实际可启动。 仓库sync及异步上传准备使用AdminJobService；容器执行属于已有Run时继续使用其执行Guard和ToolInvocation，两个所有权边界不能混用。

关键实现约束：

```python
from pathlib import Path

def resolve_workspace_path(root: Path, relative: str) -> Path:
    if Path(relative).is_absolute():
        raise ValueError("absolute path denied")
    resolved_root = root.resolve(strict=True)
    target = (resolved_root / relative).resolve(strict=True)
    if not target.is_relative_to(resolved_root):
        raise ValueError("outside workspace")
    return target
```

- [ ] **Step 4：执行上述测试命令及已完成的相关契约，确认通过。**

通过条件：发布Skill脚本在真实容器运行并下载经摘要验证产物；路径/资源/取消边界均可复现。 保留实际执行输出到阶段验收记录。

- [ ] **Step 5：检查差异并提交当前任务。**

```bash
git diff --check
git add -- src/persistence/objects/s3.py src/workspaces/repositories.py src/workspaces/snapshots.py src/tools/workspace.py src/runner/client.py src/runner/service.py src/cli/runner.py src/api/uploads.py runner/Dockerfile runner/protocol.schema.json tests/support/runner.py tests/contract/test_runner_isolation.py tests/integration/test_artifacts.py tests/e2e/test_skill_runner.py
git diff --cached
git commit -m "feat: execute pinned workspace snapshots in isolated runners"
```

仅包含本任务交付文件；某项文件未改动时从暂存命令去掉，不夹带其他项目内容。


### T15：三类专家、主图路由与真实问答/代码分析

**依赖与依据：** T14；规格 章节 5.3/5.4、7.1/7.2、8.1；AT-07/19/34/35。
**文件：** 创建或修改 `src/runtime/experts/catalog.py`、`src/runtime/experts/knowledge.py`、`src/runtime/experts/diagnosis.py`、`src/runtime/experts/code.py`、`src/runtime/graphs/dod.py`、`src/runtime/graphs/qa.py`、`src/runtime/graphs/code.py`、`src/runtime/graphs/validation.py`、`src/runtime/state/reducers.py`、`tests/support/experts.py`、`tests/integration/test_expert_budget.py`、`tests/integration/test_graph_outcomes.py`、`tests/e2e/test_qa_code_live.py`、`docs/acceptance/M2.md`。
**Interfaces：** ExpertRegistry.resolve(name, version) -> Expert；Expert.invoke(context, guard, task: ExpertTask) -> ExpertResult；GraphBuilder.build(version) -> CompiledStateGraph；EvidenceValidator.validate(scope, evidence, required_steps) -> ValidationResult。ExpertResult含subtask_id/evidence/hypotheses/unresolved_items/usage_ref。

- [ ] **Step 1：写失败测试及本任务列明的边界用例。**

```python
import pytest

@pytest.mark.asyncio
async def test_unavailable_required_evidence_prevents_success(platform):
    platform.configure_qa_evidence(status="unavailable")
    run = await platform.create_run(mode="qa")
    view = await platform.run_to_completion(run.run_id)
    assert view.status in {"partial", "failed"}
    assert view.outcome.unresolved_items
    assert not platform.report_claims_business_normal(view.outcome)
```

- [ ] **Step 2：运行测试确认失败。**

```bash
uv run pytest tests/integration/test_expert_budget.py tests/integration/test_graph_outcomes.py -v
```

预期首次因尚未实现的导入、接口或目标行为失败；夹具自身报错先修复夹具，不能把无关失败当红灯。

- [ ] **Step 3：实现最小交付。**

主图实现prepare_context→route→select_skills→collect_baseline→investigate→validate_evidence→compose_outcome→finalize，缺参/审批跳具名中断。显式模式/规则优先，模型分类只能选已发布模式。Knowledge/Diagnosis/Code使用LangChain create_agent实际工具循环，专家稳定checkpoint namespace、subtask_id、权限交集与父预算；最多2专家/4工具，父图只合结构化结果/证据引用。messages以message_id去重，hypotheses按id保留正反证据。确定性校验必需步骤、证据Scope/质量/引用有效性、上下文限额；不足则追问或partial，禁止仅主观置信度完成。测试夹具定义configure_qa_evidence/run_to_completion/report_claims_business_normal；报表判断用结构化finding而非脆弱字符串匹配。真实QA引用和固定commit文件/行号分析必须跑通，不能回退Smart/Claude当完成。保存DeepSeek工具、专家并发、预算耗尽模板、重启固定模型及Skill版本的M2记录。

关键实现约束：

```python
def merge_evidence(existing: dict, updates: dict) -> dict:
    merged = dict(existing)
    for evidence_id, evidence in updates.items():
        if evidence.evidence_id != evidence_id:
            raise ValueError("evidence identity mismatch")
        merged[evidence_id] = evidence
    return merged
```

- [ ] **Step 4：执行上述测试命令及已完成的相关契约，确认通过。**

通过条件：另运行 uv run pytest tests/e2e/test_qa_code_live.py --run-live -v；真实知识问答和仓库分析有可核对引用，专家共享预算且失败不报正常。 保留实际执行输出到阶段验收记录。

- [ ] **Step 5：检查差异并提交当前任务。**

```bash
git diff --check
git add -- src/runtime/experts/catalog.py src/runtime/experts/knowledge.py src/runtime/experts/diagnosis.py src/runtime/experts/code.py src/runtime/graphs/dod.py src/runtime/graphs/qa.py src/runtime/graphs/code.py src/runtime/graphs/validation.py src/runtime/state/reducers.py tests/support/experts.py tests/integration/test_expert_budget.py tests/integration/test_graph_outcomes.py tests/e2e/test_qa_code_live.py docs/acceptance/M2.md
git diff --cached
git commit -m "feat: compose bounded experts into the DoD execution graph"
```

仅包含本任务交付文件；某项文件未改动时从暂存命令去掉，不夹带其他项目内容。

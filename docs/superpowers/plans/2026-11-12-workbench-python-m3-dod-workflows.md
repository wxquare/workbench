---
title: "DoD Agents Python：M3 DoD 业务链路实施计划"
date: "2026-11-12"
status: ready-for-implementation
---

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 跑通真实告警与聊天、RCA/SOP/巡检、人工补充/确认、Duty Event/DoD/Jira/通知交付。
**Architecture:** 所有入口归一Run，确定性基线与Evidence校验约束专家结论，外部写操作通过Action/Outbox独立调度和对账。
**Tech Stack:** 复用M1/M2，增加SeaTalk/Jira/DoD连接器、版本化Workflow、持久化Schedule扫描。

## Global Constraints

遵守[总计划](2026-11-12-workbench-python-implementation-plan.md)。所有工具沿用同Scope时间锚点；未知外部动作结果不能自动重发；写入需持久化动作、授权与必要的具名批准。测试和目标环境资源分别验收，不从计划推断生产变更授权。

## 实施任务

### T16：SeaTalk、告警接入与多轮线程关联

**依赖与依据：** T15；规格 章节 1.2、8.2/8.3、11.2、17.3；AT-02/24。
**文件：** 创建或修改 `src/ingress/seatalk.py`、`src/ingress/alerts.py`、`src/ingress/routing.py`、`src/api/ingress.py`、`src/connectors/seatalk.py`、`src/persistence/source_events.py`、`tests/support/ingress.py`、`tests/integration/test_ingress.py`、`tests/e2e/test_seatalk_conversation.py`。
**Interfaces：** IngressService.accept(connector_id, raw_body, headers) -> IngressReceipt；MessageRouter.route(verified_event) -> NewRunCommand | ResumeCommand | ChallengeResponse；SourceEventStore.accept(uow, team_id, connector_id, external_event_id) -> bool。

- [ ] **Step 1：写失败测试及本任务列明的边界用例。**

```python
import pytest

@pytest.mark.asyncio
async def test_duplicate_alert_creates_one_run(platform):
    payload = platform.signed_alert(external_event_id="alert-001", region="ID")
    first = await platform.ingress.accept(**payload)
    again = await platform.ingress.accept(**payload)
    assert first.run_id == again.run_id
    assert await platform.source_run_count("alert-001") == 1
```

- [ ] **Step 2：运行测试确认失败。**

```bash
uv run pytest tests/integration/test_ingress.py tests/e2e/test_seatalk_conversation.py -v
```

预期首次因尚未实现的导入、接口或目标行为失败；夹具自身报错先修复夹具，不能把无关失败当红灯。

- [ ] **Step 3：实现最小交付。**

分别实现SeaTalk平台challenge/回调响应与告警Webhook，签名算法按实际连接器协议校验原始body、时间戳与防重放窗口，不假定两者同一算法。规范化external/thread/message/actor/region/env/service/rule/anchor/source_url，正文和链接冲突保留。来源去重与Run受理使用同一UoW，唯一键按team/connector/external_event；失败事务不得留下“已接入但无Run”。线程映射按连接器来源持久化，多轮新问题排队，用户补充只映射具名interrupt；无明确关联不能自动填最近等待点。来源事件90d保留，7d以前走显式补录，避免重放旧告警。tests/support/ingress.py定义signed_alert/source_run_count，使用测试密钥/本地签名验证，不存真实令牌。SeaTalk真实测试验证challenge、重复消息、正确线程回复、错误actor、断线重投和具名恢复；通知发送仍走T19 Outbox，不在回调同步发。

关键实现约束：

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class SourceEventKey:
    team_id: str
    connector_id: str
    external_event_id: str
```

- [ ] **Step 4：执行上述测试命令及已完成的相关契约，确认通过。**

通过条件：离线接入契约通过；真实SeaTalk测试以 --run-live 复跑，线程和等待点关联可查。旧新入口唯一主动路由在T23复验。 保留实际执行输出到阶段验收记录。

- [ ] **Step 5：检查差异并提交当前任务。**

```bash
git diff --check
git add -- src/ingress/seatalk.py src/ingress/alerts.py src/ingress/routing.py src/api/ingress.py src/connectors/seatalk.py src/persistence/source_events.py tests/support/ingress.py tests/integration/test_ingress.py tests/e2e/test_seatalk_conversation.py
git diff --cached
git commit -m "feat: normalize signed chat and alert ingress"
```

仅包含本任务交付文件；某项文件未改动时从暂存命令去掉，不夹带其他项目内容。


### T17：固定 Scope 的告警 RCA 与证据完整性

**依赖与依据：** T16；规格 章节 5.2/5.4、7.1/7.2、8.2、9.2；AT-05/06。
**文件：** 创建或修改 `src/runtime/graphs/rca.py`、`src/runtime/graphs/context.py`、`src/runtime/graphs/baseline.py`、`src/domain/query_scope.py`、`src/incidents/history.py`、`tests/support/rca.py`、`tests/integration/test_query_scope.py`、`tests/integration/test_rca_evidence.py`、`tests/e2e/test_alert_rca_live.py`。
**Interfaces：** ContextResolver.revise_scope(auth, context_ref, user_response) -> QueryScopeRevision；BaselineCollector.collect(context, guard, scope_ref) -> list[Evidence]；RcaGraph.invoke(run_context, guard) -> RunOutcome。所有ConnectorRequest必须携带scope_ref、anchor/window/environment/region/service。

- [ ] **Step 1：写失败测试及本任务列明的边界用例。**

```python
import pytest

@pytest.mark.asyncio
async def test_scope_conflict_waits_then_collects_one_time_window(platform):
    run = await platform.create_conflicting_alert(body_region="ID", link_region="PH")
    waiting = await platform.run_until_waiting(run.run_id)
    assert waiting.waiting.required_fields == ["region"]
    await platform.answer_scope(waiting, region="ID")
    view = await platform.run_to_completion(run.run_id)
    evidence = await platform.current_scope_evidence(run.run_id)
    assert len({item.time_window for item in evidence}) == 1
    assert {item.region for item in evidence} == {"ID"}
    assert view.run_id == run.run_id
```

- [ ] **Step 2：运行测试确认失败。**

```bash
uv run pytest tests/integration/test_query_scope.py tests/integration/test_rca_evidence.py -v
```

预期首次因尚未实现的导入、接口或目标行为失败；夹具自身报错先修复夹具，不能把无关失败当红灯。

- [ ] **Step 3：实现最小交付。**

先查询权威告警详情解决正文/URL冲突，仍不明才await_input；固定业务时间锚点后监控/日志/Trace/变更沿用相同窗。必需baseline确定性运行，Knowledge与Diagnosis有限并行，按需Code关联commit/文件行号；保留假设支持/反对证据、影响和未完成项。历史事件复用核对team/rule/service/env/window并记录来源，不能拿不同范围历史正常结论替代实时结果。补充范围只由可信ContextResolver生成新ScopeRevision，旧Evidence保留历史但不作为当前有效证据。quality valid/empty/unavailable/conflicting/stale分开，业务正常需真实有效检查，200/空数组不能断定正常。tests/support/rca.py定义冲突告警、answer_scope/current_scope_evidence；真实测试至少一套监控/日志、Trace和代码仓库，与10个已知根因案例相关联；未知根因明确不确定，不编造根因标签。

关键实现约束：

```python
def evidence_matches_scope(evidence, scope) -> bool:
    return (
        evidence.scope_ref == scope.ref
        and evidence.region == scope.region
        and evidence.environment == scope.environment
        and evidence.service == scope.service
        and evidence.time_window == scope.time_window
    )
```

- [ ] **Step 4：执行上述测试命令及已完成的相关契约，确认通过。**

通过条件：另运行 uv run pytest tests/e2e/test_alert_rca_live.py --run-live -v；告警证据同锚点、引用可核对，冲突追问恢复同一Run。 保留实际执行输出到阶段验收记录。

- [ ] **Step 5：检查差异并提交当前任务。**

```bash
git diff --check
git add -- src/runtime/graphs/rca.py src/runtime/graphs/context.py src/runtime/graphs/baseline.py src/domain/query_scope.py src/incidents/history.py tests/support/rca.py tests/integration/test_query_scope.py tests/integration/test_rca_evidence.py tests/e2e/test_alert_rca_live.py
git diff --cached
git commit -m "feat: execute evidence based alert RCA with immutable query scopes"
```

仅包含本任务交付文件；某项文件未改动时从暂存命令去掉，不夹带其他项目内容。


### T18：版本化 SOP、变量分支与巡检调度

**依赖与依据：** T17；规格 章节 7.3/7.4、14.4、15.4；AT-10/11。
**文件：** 创建或修改 `src/domain/workflows.py`、`src/domain/schedules.py`、`src/runtime/graphs/sop.py`、`src/runtime/graphs/inspection.py`、`src/jobs/schedules.py`、`src/application/schedules.py`、`src/api/schedules.py`、`src/persistence/migrations/versions/0002_workflows.py`、`config/workflows/payment-check.yaml`、`tests/support/workflows.py`、`tests/integration/test_sop.py`、`tests/integration/test_schedules.py`。
**Interfaces：** WorkflowService.publish(auth, definition: WorkflowDefinition) -> WorkflowVersion；SopExecutor.compile(version) -> CompiledStateGraph；ScheduleService.trigger(auth, schedule_id, key) -> AcceptedRun；ScheduleScanner.tick(now_ms) -> list[ScheduleFire]。

- [ ] **Step 1：写失败测试及本任务列明的边界用例。**

```python
import pytest

@pytest.mark.asyncio
async def test_required_sop_step_failure_survives_summary(platform):
    workflow = await platform.publish_workflow(
        steps=[{"step_id": "logs", "required": True, "tool": "logs.search"}])
    platform.tool_transport.raise_timeout_for("logs.search")
    run = await platform.create_workflow_run(workflow, mode="sop")
    view = await platform.run_to_completion(run.run_id)
    assert view.status in {"partial", "failed"}
    assert view.outcome.unresolved_items
    assert view.workflow_step_results["logs"].status == "timeout"
```

- [ ] **Step 2：运行测试确认失败。**

```bash
uv run pytest tests/integration/test_sop.py tests/integration/test_schedules.py -v
```

预期首次因尚未实现的导入、接口或目标行为失败；夹具自身报错先修复夹具，不能把无关失败当红灯。

- [ ] **Step 3：实现最小交付。**

补workflow_versions表（规格定义WorkflowVersion但章节 13.2未单列该表），不可变version/hash、step_id/DAG/输入映射/输出Schema/条件/timeout/required/failure_policy及审计；版本保留与Run引用规则一致。P0先通过bootstrap中config/workflows的版本化种子发布固定工作流，不扩写未要求的可视化编排器。步骤结果按step_id合并，变量具名传递，禁止无界回边；bounded investigation显式预算。必需失败只partial/failed/人工，summary不能洗掉。实现schedule API创建/分页/启停/手动触发的幂等/revision，IANA时区/cron（以锁定croniter版本验证DST）、UTC due_at唯一约束、forbid重叠、6h内只合并最近一次/超窗missed，独立Thread。schedule_fires新增明确fire_status字段支持missed。双Scheduler竞争只一Run、暂停/手动触发/时区夏令时/恢复优先公平性均实测。tests/support/workflows.py提供publish_workflow/create_workflow_run及可控StorageClock；不以进程内timer代替持久化扫描。

关键实现约束：

```python
def validate_dag(nodes: set[str], edges: list[tuple[str, str]]) -> None:
    from graphlib import TopologicalSorter
    dependencies = {node: set() for node in nodes}
    for source, target in edges:
        if source not in nodes or target not in nodes:
            raise ValueError("unknown workflow step")
        dependencies[target].add(source)
    tuple(TopologicalSorter(dependencies).static_order())
```

- [ ] **Step 4：执行上述测试命令及已完成的相关契约，确认通过。**

通过条件：固定SOP分支/变量/有限并行与失败传播可追踪；重复due_at、错过合并、overlap及DST契约通过。 保留实际执行输出到阶段验收记录。

- [ ] **Step 5：检查差异并提交当前任务。**

```bash
git diff --check
git add -- src/domain/workflows.py src/domain/schedules.py src/runtime/graphs/sop.py src/runtime/graphs/inspection.py src/jobs/schedules.py src/application/schedules.py src/api/schedules.py src/persistence/migrations/versions/0002_workflows.py config/workflows/payment-check.yaml tests/support/workflows.py tests/integration/test_sop.py tests/integration/test_schedules.py
git diff --cached
git commit -m "feat: run versioned SOP and deduplicated inspections"
```

仅包含本任务交付文件；某项文件未改动时从暂存命令去掉，不夹带其他项目内容。


### T19：Duty Event、动作确认、Outbox 与未知结果对账

**依赖与依据：** T18；规格 章节 10.2、11.2、14.3/14.5、16.3/16.4、17.2；AT-17/18/21。
**文件：** 创建或修改 `src/domain/actions.py`、`src/application/actions.py`、`src/incidents/service.py`、`src/delivery/dispatcher.py`、`src/delivery/reconcile.py`、`src/connectors/jira.py`、`src/connectors/duty_actions.py`、`src/api/incidents.py`、`src/api/actions.py`、`src/cli/dispatcher.py`、`tests/support/actions.py`、`tests/fault/test_action_unknown.py`、`tests/integration/test_approvals.py`、`tests/integration/test_outbox.py`、`tests/e2e/test_delivery_live.py`。
**Interfaces：** ActionService.propose(context, proposal: ActionProposal) -> Action；authorize(auth, approval_id, decision, expected_args_hash) -> Action；Dispatcher.dispatch_one() -> DispatchResult；ActionReconciler.reconcile(auth, action_id, fact: ExternalReceipt | None) -> Action。ActionGuard与执行Guard区分。

- [ ] **Step 1：写失败测试及本任务列明的边界用例。**

```python
import pytest

@pytest.mark.asyncio
async def test_response_loss_becomes_unknown_without_resend(platform):
    action = await platform.authorized_jira_action()
    platform.jira.simulate_success_then_lost_response()
    await platform.dispatcher.dispatch_one()
    await platform.dispatcher.dispatch_one()
    current = await platform.actions.get(platform.auth, action.action_id)
    assert current.state == "unknown"
    assert platform.jira.write_attempts == 1
    reconciled = await platform.reconcile_from_external_fact(action.action_id)
    assert reconciled.state == "succeeded"
    assert reconciled.receipt.external_id
```

- [ ] **Step 2：运行测试确认失败。**

```bash
uv run pytest tests/integration/test_approvals.py tests/integration/test_outbox.py tests/fault/test_action_unknown.py tests/fault/test_redis_outage.py -v
```

预期首次因尚未实现的导入、接口或目标行为失败；夹具自身报错先修复夹具，不能把无关失败当红灯。

- [ ] **Step 3：实现最小交付。**

ResultService.finalize接通真实Duty Event、incident_links、提议动作和Outbox同事务；DoD查询已在T12，跟进/业务写在此注册为动作。稳定action_key/action_id/args_hash请求前持久化，审批绑定工具版本/范围/参数/策略/主体/30min，变参数需新批准，拒绝可继续Run解释结果。Dispatcher短事务claim Action/Outbox租约，事务外执行前重查当前权限和批准有效性，撤权failed OPERATION_DENIED/outbox dead。Jira/通知支持幂等键则复用，否则使用关联字段查询；lost response→unknown且禁自动重发，可靠事实才能改成功/失败。明确暂时失败最多8总尝试按规格间隔，永久错误dead，人工重试保留原id；outbox也有unknown。签名通知目标只来自绑定渠道，回执独立追加事件，不改分析终态。tests/support/actions.py实现authorized_jira_action与独立Jira模拟器/查询事实；真实DoD、Jira测试项目和SeaTalk渠道按授权实际连通，记录external_id。恢复备份默认暂停Dispatcher，先对账历史unknown及sending。 ActionGuard定义action_id/outbox_id/owner/lease_revision/expires_at并在写回执事务内CAS校验；模型/Graph执行Guard不能代替已终态Run的独立交付租约。

关键实现约束：

```python
import hashlib
import json

def action_payload_hash(tool_version: str, target: dict, arguments: dict) -> str:
    canonical = json.dumps(
        {"tool_version": tool_version, "target": target, "arguments": arguments},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
```

- [ ] **Step 4：执行上述测试命令及已完成的相关契约，确认通过。**

通过条件：另运行 uv run pytest tests/e2e/test_delivery_live.py --run-live -v；事件/Jira/通知真实回执关联同Run，未知动作不盲重发。 保留实际执行输出到阶段验收记录。

- [ ] **Step 5：检查差异并提交当前任务。**

```bash
git diff --check
git add -- src/domain/actions.py src/application/actions.py src/incidents/service.py src/delivery/dispatcher.py src/delivery/reconcile.py src/connectors/jira.py src/connectors/duty_actions.py src/api/incidents.py src/api/actions.py src/cli/dispatcher.py tests/support/actions.py tests/fault/test_action_unknown.py tests/integration/test_approvals.py tests/integration/test_outbox.py tests/e2e/test_delivery_live.py
git diff --cached
git commit -m "feat: deliver auditable business actions with reconciliation"
```

仅包含本任务交付文件；某项文件未改动时从暂存命令去掉，不夹带其他项目内容。


### T20：真实 DoD 全链路与 M3 阶段验收

**依赖与依据：** T19；规格 章节 1.2、8、23.1/23.3；AT-02/05/06/10/11/17/18。
**文件：** 创建或修改 `tests/e2e/test_dod_journey_live.py`、`tests/e2e/test_alert_rca_live.py`、`tests/e2e/test_delivery_live.py`、`tests/e2e/test_seatalk_conversation.py`、`tests/fixtures/business/manifest.json`、`docs/acceptance/connectors.md`、`docs/acceptance/M3.md`、`docs/runbooks/action-reconciliation.md`。
**Interfaces：** 复用所有生产 /v1/ingress、Run/resume/events、Incident/Action接口；live_environment fixture提供已授权测试团队/项目/渠道的secret_ref与版本，不在测试中替换生产连接器。 live fixture的wait_for_outcome()返回JourneyResult(run_id,evidence_refs,incident_id)，通过Run与Incident API组合，不给生产RunOutcome凭空增加incident_id字段。

- [ ] **Step 1：写失败测试及本任务列明的边界用例。**

```python
import pytest

@pytest.mark.live
@pytest.mark.asyncio
async def test_real_alert_to_followup(live_environment):
    journey = await live_environment.submit_reviewed_alert()
    await journey.answer_named_interrupt_if_present()
    outcome = await journey.wait_for_outcome()
    receipts = await journey.wait_for_test_channel_receipts()
    assert outcome.evidence_refs
    assert outcome.incident_id
    assert receipts.jira_external_id
    assert receipts.seatalk_message_id
    assert receipts.run_id == outcome.run_id
```

- [ ] **Step 2：运行测试确认失败。**

```bash
uv run pytest tests/e2e/test_dod_journey_live.py tests/e2e/test_alert_rca_live.py tests/e2e/test_delivery_live.py tests/e2e/test_seatalk_conversation.py --run-live -v
```

预期首次因尚未实现的导入、接口或目标行为失败；夹具自身报错先修复夹具，不能把无关失败当红灯。

- [ ] **Step 3：实现最小交付。**

组合SeaTalk多轮/告警、同锚点监控日志Trace、Knowledge/Code、Skill Runner产物、范围冲突人工恢复、SOP巡检、DutyEvent、DoD、Jira与通知。真实fixture实现submit_reviewed_alert/answer_named_interrupt_if_present/wait_for_outcome/wait_for_test_channel_receipts，输入来自人工审核案例，动作目标严格测试绑定。不能让if_present掩盖人工恢复验收：额外显式冲突案例断言等待出现并继续同run_id。各连接记录配置/发现/真实调用三个级别，不把发现成功当业务成功；缺任何真实知识、监控/日志、Trace、Git、值班、Jira、SeaTalk均保持未通过。故障验证中途kill Worker后不重复Action、Redis全断能投递、外部写lost response可查事实。30案例manifest补人工标签和来源审核人，暂未审核不计质量分。整理操作对账手册和M3完整演示脚本。

关键实现约束：

```python
def validate_live_connector_record(record: dict) -> None:
    required = {"connector", "environment", "version", "observed_at",
                "request_hash", "receipt_or_evidence_ref"}
    missing = required - record.keys()
    if missing:
        raise ValueError(f"missing live acceptance fields: {sorted(missing)}")
```

- [ ] **Step 4：执行上述测试命令及已完成的相关契约，确认通过。**

通过条件：至少一条完整真实诊断交付链路及一条强制人工输入链路通过；所有连接都有实际证据和独立回执。 保留实际执行输出到阶段验收记录。

- [ ] **Step 5：检查差异并提交当前任务。**

```bash
git diff --check
git add -- tests/e2e/test_dod_journey_live.py tests/e2e/test_alert_rca_live.py tests/e2e/test_delivery_live.py tests/e2e/test_seatalk_conversation.py tests/fixtures/business/manifest.json docs/acceptance/connectors.md docs/acceptance/M3.md docs/runbooks/action-reconciliation.md
git diff --cached
git commit -m "test: verify real DoD diagnosis and delivery journeys"
```

仅包含本任务交付文件；某项文件未改动时从暂存命令去掉，不夹带其他项目内容。

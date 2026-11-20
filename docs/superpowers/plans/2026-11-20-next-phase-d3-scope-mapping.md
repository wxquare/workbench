# Workbench 声明式范围映射 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 后置交付受部署授权约束的声明式参数映射，使新增兼容的只读工具可以通过配置接入。

**Architecture:** 绑定声明模型参数、查询范围、部署grant与运行context分别映射到完整工具schema的哪些位置；部署配置钉住允许的工具和映射hash。执行先收窄授权再组装参数，再用发布schema校验，继续走PolicyToolExecutor。

**Tech Stack:** 现有Pydantic/JSON Schema/MCPAdapter/pytest；不执行表达式、模板代码、脚本或任意JSONPath。

## Global Constraints

- 继承[总计划](2026-11-20-next-phase.md)。本包为**后置可选**，估算12–18 Agent h，不阻塞用户确认的单knowledge_search首版。
- “声明只能收窄、不能放宽”授权；部署read_tools和scope_grants仍是最终约束。
- 旧DoD四工具knowledge_search/metrics_query/logs_search/trace_get迁移需有行为对照；不能改变历史绑定hash或旧Run恢复含义。
- 模型参数仍限闭合对象+标量；完整上游参数可由受控映射构造scope/context对象。
- 不自动接入第二业务工具；用户明确选择服务器/工具/范围后才配置真实授权并验证通用性。

## 1 新契约

Create `apps/agent/src/workbench_agent/tools/scope_mapping.py`，在此定义：

```python
from typing import Literal
from pydantic import BaseModel, ConfigDict

class FieldMapping(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')
    origin: Literal['model', 'query_scope', 'grant', 'context']
    source: str
    target: tuple[str, ...]

class ArgumentMapping(BaseModel):
    model_config = ConfigDict(frozen=True, extra='forbid')
    schema_version: Literal['scope-map-v1'] = 'scope-map-v1'
    fields: tuple[FieldMapping, ...]
    model_input_schema: dict
```

增加纯函数 `validate_mapping(mapping: ArgumentMapping, full_schema: dict, deployment_policy: dict) -> None`、`bind_arguments(mapping: ArgumentMapping, model_args: dict, *, query_scope: dict, grant: dict, context: dict) -> dict`、`mapping_hash(mapping: ArgumentMapping) -> str`。source只允许列举的点分字段名，target是最多4段对象属性路径；不支持数组下标、通配、条件、函数或表达式。

例：knowledge_search 的 query/scene/limit来自model；scope.service/environment/region来自query_scope；scope.team/source_id来自对应部署grant；context.run_id/invocation_id/query_scope_ref来自服务端context。时间字段沿既有grant.timezone和窗口规则转换，不由绑定自选时区。

ToolBinding增加 `argument_mapping: ArgumentMapping|None`，序列化采用与当前FrozenModel一致的 `exclude_if=lambda value: value is None`。旧JSON缺字段时保持缺字段，确保旧hash不因补null变化；从不原地修改已发布行。

部署MCPConnectionBinding的授权规则增加可选 `mapping_policies`，每项包含tool_name、mapping_hash、允许model源字段、允许query_scope/grant/context源字段与受保护target路径。配置钉hash，不能靠绑定自己声称它安全；未配新策略仅允许既有legacy映射路径。

## Task D3.1：映射模型、发布验证与纯编译器（6–9 h）

**Files:** Create `apps/agent/src/workbench_agent/tools/scope_mapping.py`、`tests/unit/test_scope_mapping.py`、`tests/contract/test_scope_mapping_policy.py`；Modify `domain/tools.py`、`settings.py`、`tools/registry.py`、`tools/model_schema.py`、`docs/06-reference/03-configuration.md`、`docs/05-decisions.md`。

**Interfaces:** 使用§1的三个纯函数与类型；D1的validate_managed_binding调用validate_mapping，Profile发布和执行时再次校验；ToolRegistry的旧Action内部发布协议保留。D1读取接口返回绑定声明和模型投影，但仍不暴露secret和受保护部署配置全文。

- [ ] **Step 1：先测试不能扩权的反例。** 模型输入含scope/team/context/credential或未声明字段直接拒绝；两个source写同target、父子路径重叠、未知grant字段、缺必需target、目标类型不符、映射hash不在部署允许列表均拒绝。模型字符串含表达式只作为普通文本，无执行路径。旧ToolBinding dump/hash与当前JSON逐字canonical相同。
- [ ] **Step 2：红灯。** `cd apps/agent && uv run python -m pytest tests/unit/test_scope_mapping.py tests/contract/test_scope_mapping_policy.py -q`。
- [ ] **Step 3：实现有界编译器。** 声明≤64字段，target深度≤4，字段名使用明确白名单规则；原值按来源分别读取，受保护scope/context/授权字段不能origin=model。组装前复用已有查询范围和grant交集检查，组装后用full_schema再验；任何未知source或缺值都拒绝，不回退模型自己填scope，不接受任意literal身份字段。
- [ ] **Step 4：发布/恢复版本兼容。** 新发布schema-map-v1绑定通过hash钉部署策略；旧绑定无声明走legacy adapter。Profile固定新binding ref和hash；部署撤销mapping或hash改变时，旧Run后续发送被拒绝，不临时套新映射完成旧检查点。对已成功调用的读取依旧按原receipt和当前权限复核。
- [ ] **Step 5：绿灯与文档。** 测所有origin、参数缺失/附加、路径冲突、schema错配、授权撤销和旧hash不变。建议 `feat: compile deployment-approved tool scope mappings`。

## Task D3.2：MCP执行接线、旧工具对照与配置式接入验证（6–9 h）

**Files:** Modify `apps/agent/src/workbench_agent/tools/mcp.py`、`tools/policy.py`、`runtime/experts/knowledge.py`、`runtime/graphs/evidence_authority.py`、`tools/model_schema.py`；Create `tests/contract/test_declarative_mcp_mapping.py`；Modify `tests/contract/test_dod_mcp_mapping.py`、`test_tool_policy.py`、`apps/web/src/components/AgentTools.vue`；同步 `docs/02-architecture.md`、`03-development.md`、`06-reference/01-api.md`、`03-configuration.md`。

**Interfaces:** `MCPAdapter.prepare`增加keyword-only绑定映射信息，由已验证ToolBinding提供；discover/call仍通过ConnectionPolicy。新映射的resource identity必须能供PolicyToolExecutor receipt和authorized_sources复核，不能只把args组装出来就绕过证据授权。

- [ ] **Step 1：先写旧新等价测试。** 对DoD四工具，用同一scope/grant/correlation/model_args调用旧build_dod_arguments与新映射，比较完整canonical JSON、时间转换、scope/context、身份键；越界窗口、DST歧义、source未授权等拒绝结果也等价。原build_business_arguments的业务工具不在本轮无依据迁移。
- [ ] **Step 2：红灯。** `cd apps/agent && uv run python -m pytest tests/contract/test_declarative_mcp_mapping.py tests/contract/test_dod_mcp_mapping.py -q`。
- [ ] **Step 3：接MCP与Policy。** 新映射路径必须匹配部署read_tools+mapping_policy，无需加BUSINESS_TOOLS白名单；无声明仍执行旧代码。同时把D2首批knowledge_search限制移为部署 `tool_conversation_allowed_tools` 配置，默认仍仅knowledge_search；新增工具须同时获该配置、read_tools、mapping_policy与Profile授权，不能只改页面下拉选项。模型schema从绑定声明编译，完整参数再校验、当前grant交集再复核；同一logical_call_id的组装参数/范围变化触发冲突，不以“更新配置”重发请求。
- [ ] **Step 4：迁移现有四工具配置。** 发布新的声明式绑定版本，更新试验Agent的Profile引用，旧绑定和旧函数保留以恢复历史Run。知识工具仍不向内部KB传时间窗；其他三工具保留既有时区、时间上界和范围约束。证据身份结构与receipt复核使用同样授权，不能因工具名不在硬编码集合而跳过。
- [ ] **Step 5：验证配置式新工具。** 离线测试注册一个未出现在Python白名单中的`test_read_counter` MCP工具，闭合标量模型schema；仅通过部署policy+新绑定配置完成discover→publish→call→evidence→read，再证明移除授权立即拒绝。这个测试证明“无需改Python工具名白名单”的接口能力，不代表真实业务工具已经验收；真实第二工具等用户选择后另采业务用例。
- [ ] **Step 6：绿灯和交付。** 跑四工具映射、scope、tool_policy、证据读取、旧知识/RCA/SOP定向契约和新图恢复；管理页展示来源和受保护字段。建议 `feat: enable configured read-only tool scope mapping`。交付旧/新对照报告及配置接入指南，明确历史fallback保留原因。

## 本包验收

- [ ] 新只读工具不改Python白名单即可走受权映射和证据协议。
- [ ] 声明无法改变部署授权、模型无法覆盖范围或身份；撤销配置可阻止新发送。
- [ ] 四个旧DoD工具参数和拒绝行为等价；旧绑定hash/历史Run恢复不变。
- [ ] 未提供真实第二工具时只声明配置协议验证完成，业务通用性仍待真实验证。

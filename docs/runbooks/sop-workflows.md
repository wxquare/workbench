---
title: "SOP：固定工作流和巡检计划"
---

## 工作流种子

显式执行 `dod bootstrap --config <path>` 时，从部署配置的 `asset_root/config/workflows/` 读取 `.yaml` 种子。文件名须与 `name` 一致。只创建缺失的工作流；修改文件不会覆盖已发布版本或已受理 Run。种子目录和文件不得为符号链接，单文件最大 64 KiB，最多 32 个文件。

示例 [payment-check.yaml](../../config/workflows/payment-check.yaml) 使用 `metrics_query` 和 `logs_search` 两个工具别名。部署时须发布同名只读工具绑定、配置相同的授权业务范围及 `diagnosis` 工具权限；工作流本身不授予工具权限。

## 条件和输出

`dod.v12` 支持引用直接依赖步骤的标量字段，通过 `condition.source`、`condition.equals` 和可选 `condition.negate` 决定是否执行。条件不满足的步骤记录为 `skipped`；引用不存在时记录为 `invalid_input`。跨步骤输入通过 `inputs` 显式映射，例如 `service: metrics.service`；固定参数放在 `arguments`，两者不能重名。

`output_schema` 支持有限的 JSON Schema 结构类型、对象属性、数组项、数值范围和枚举；不支持引用、正则或组合运算。每个步骤输出不符合 Schema 时记录 `invalid_output`，必需步骤失败不会被摘要隐藏。`dod.v11` 不支持条件或输出 Schema，受理时会拒绝包含这些字段的工作流；已有 v11 Run 保持原有实现。

## 运行限制

Run 和巡检计划须引用已发布的工作流版本，Run 受理时固定其版本和摘要。部署的工具权限、查询范围和预算仍逐步检查；跨来源业务范围不一致会在派发前拒绝。缺少 Scope、工具或权限时记录未完成，当前结果始终为 `partial`，不能当作业务正常的证明。

当前执行按 DAG 顺序串行进行，不提供可视化编排、动态循环或任意脚本执行。巡检计划可通过 `/v1/agents/{agent_id}/schedules` 创建、分页、启停和手动触发；生产 Scheduler 使用数据库持久化的到期时间与触发记录。配置 Scope 时，时间窗以预定 `due_at` 为锚点，手动触发以数据库当前时间为锚点。

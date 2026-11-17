---
title: "RCA 基线：固定范围与本地验证"
---

## 使用范围

`dod.v10` 在用户确认地区后，将监控、日志和 Trace 查询绑定到同一 Run 的当前范围和固定时间窗。它使用已发布 Profile 中各一个只读 `metrics_query`、`logs_search` 和 `trace_get` 工具；缺少或重复绑定时对应检查不可用。调用仍受当前主体、团队、Agent、Skill、专家权限、连接器授权和父 Run 预算限制。

部署配置 `service.rca_metric_template` 必须显式指定获准的监控模板；默认未配置，监控检查不可用。Trace 查询要求输入的 `business_context.trace_id`，缺失时不会猜测。连接器还必须配置允许的团队、区域、服务、环境、时间窗、来源和模板；无匹配授权时不会发送请求。 基线在首次派发前固定并比较各连接器授权得到的业务团队、环境、区域、服务和 UTC 时间窗；不同来源可以使用不同 source_id，但这些业务范围必须一致。授权变更导致范围不一致时停止采集并返回 partial，历史缺少业务范围绑定的收据不会用于恢复或读取。入站需要提供带时区的 `anchor`、正数 `window_seconds`（最多 3600 秒）、`service`、`environment` 以及 `region` 或冲突的 `body_region` 和 `link_region`。

执行结果区分 `empty`、`unavailable`、`timeout` 等状态；空结果和成功检索均不能单独证明业务正常。当前图只收集有来源的基线证据并返回 partial，不生成未经验证的根因或完整结论。证据读取仍检查当前权限和 Scope；范围更改后的旧证据不用于当前结果。

## 验证边界

本地集成测试使用项目内 `apps/mcp-server` 的真实 MCP 协议及合成上游数据。测试验证监控、日志和 Trace 使用同一范围与时间窗、预算和撤权处理，以及收据完成后 Worker 崩溃不会重发。它不代表真实企业监控或日志系统已接入。

---
title: "M2 MCP：部署和验证"
---

## 部署边界

`service.mcp_connections` 默认为空。每个条目必须由部署管理员配置 `team_id`、`workspace_id`、`server`、`credential_scope`、HTTPS `endpoint`、`secret_ref` 和 `read_tools`。同一 server 和 credential_scope 不能绑定到不同端点或密钥引用。每次发现或调用先按团队、工作空间及连接检查；调用还检查部署的只读工具名单。凭据只保存在外部密钥引用中，客户端连接时解析，不记录凭据值。例子中的标识仅供说明，不包含实际服务器或凭据：

```yaml
service:
  mcp_connections:
    - team_id: "<team UUID>"
      workspace_id: "<workspace UUID>"
      server: "knowledge-nonproduction"
      credential_scope: "knowledge-read"
      endpoint: "https://<approved-nonproduction-host>/mcp"
      secret_ref: "env://MCP_READ_TOKEN"
      read_tools: ["KnowledgeRetrieveV2"]
```

工作进程可通过 `tools.mcp.configured_adapter(settings.service.mcp_connections, SecretProvider())` 创建官方 MCP Streamable HTTP 适配器。工具权限由部署注入的可信策略提供，不能从模型参数取得。工具参数必须显式包含当前查询范围的全部字段并相等；缺少明确映射时安全拒绝。新工具版本要添加到 Workspace 的已发布运行配置才能用于新 Run。管理 API 提交持久 probe Job；Worker 默认只发现并比较 Schema，只有配置固定 probe 请求后才会调用。

## 已验证与待验证

本地自动测试使用官方 FastMCP 服务和 ASGI transport，覆盖真实 MCP 协议初始化、工具发现、只读工具调用、结构化结果、预算预留以及 Evidence 作用域。它不证明外部网络、TLS、企业身份验证或业务数据质量。

外部知识、监控、日志、Trace 与 DoD 业务调用尚未验证：当前没有配置经授权的非生产端点、服务认证与团队/区域范围。不得将本地服务测试作为外部业务验收。`tests/fixtures/business/manifest.json` 是待人工审核的合成问题候选，不含答案标签，也不是业务质量证据。

如需在获授权的非生产环境执行真实只读验收，先准备仅包含密钥引用的 JSON 案例文件，并设置 `WORKBENCH_MCP_LIVE_CASES_FILE` 指向该文件。顶层五个键分别是 `knowledge`、`monitoring`、`logs`、`traces`、`dod`；每个值包含 `connection`（部署绑定）、`binding`（与发现结果一致的工具配置）、`scope`（审批范围）和 `args`（该范围内的请求）。未配置时 live 项默认跳过。测试会实际读取业务数据；只在目的地、身份、范围及查询均获得授权后运行。

## 安全修订状态

管理员 probe API 提交持久化 Job，实际连接仅由 Worker 执行。默认只执行发现与 Schema 检查；仅当部署明确配置对应工具的固定 `probe_requests` 时才尝试调用。API 不解析 MCP 密钥。

生产部署必须配置 `service.tool_permissions` 的 `users`、`teams`、`workspaces`、`skills`、`experts` 五个映射，缺失即拒绝；Skill 键为 `resource_id:version:content_hash`，没有 Skill 时必须显式允许 `__none__`。企业业务工具还要求精确 `scope_grants`。当前只实现受信范围下的日志查询和 DoD 团队查询；知识、监控及 Trace 由于尚不能证明目标范围，保持拒绝。日志不允许 session_key 或会话注入，防止触发 SeaTalk 消息发送。

Evidence 保存有界脱敏内容，超限内容只保存节选并标记 `truncated`；当前规则屏蔽常见敏感键和 Bearer 字符串，不能代替数据源专属的完整敏感信息治理。

Worker 必须向工具执行器提供当前 Worker 发放的 Execution；调用方不能覆盖权限提供器或专家身份。当前实现只允许可信 `diagnosis` 身份，其他专家身份需要后续受信子图绑定。DoD 只反映观察时当前值班人，历史窗口被拒绝；证据记录观察时间。

范围授权可指定固定 `scope`，也可使用 `allowed_scope`（service/environment/region）、`earliest`、`latest` 和 `max_span_seconds` 限制可变查询窗口。工具 probe 的业务请求需在对应部署连接中配置 `probe_requests`，其值为 `{scope: ..., args: ...}`，与业务调用使用相同范围映射。Knowledge 的 task_context 目前只能证明参与结果匹配和排序，不能证明团队/区域授权过滤，因此继续拒绝；监控与 Trace 也维持拒绝。

工具权限策略由 Worker 的部署 composition root 构造并绑定 backend，Worker 构造不接受调用方注入的 provider。只有 Worker 实际领取中的 claim 才获得工具 Execution，结束后权限失效。该边界防止应用不受信请求参数替换权限，不提供同一 Python 进程内任意恶意代码的隔离；后者需要独立进程和外部授权服务。

## 本地 DoD MCP 服务接入

`workbench-mcp-server` 提供 `knowledge_search`、`metrics_query`、`logs_search` 和 `trace_get`。这些名称与企业旧工具契约不同，不能复用旧工具的范围映射。服务端必须自身对完整资源元组强制授权；Agent 的部署绑定同时固定客户端团队、Workspace、源 ID、业务团队和查询范围。示例均为占位测试值：

```yaml
service:
  mcp_connections:
    - team_id: "<agent-team-uuid>"
      workspace_id: "<workspace-uuid>"
      server: "dod-local"
      credential_scope: "dod-local-read"
      endpoint: "http://127.0.0.1:8000/mcp"
      allow_loopback_http: true
      secret_ref: "env://WORKBENCH_MCP_TOKEN"
      read_tools: ["knowledge_search", "metrics_query", "logs_search", "trace_get"]
      scope_grants:
        - kind: "dod-mcp-v1"
          team: "<business-team>"
          allowed_scope: {service: "<service>", environment: "test", region: "<region>"}
          earliest: "2026-01-01 00:00:00"
          latest: "2027-01-01 00:00:00"
          max_span_seconds: 3600
          timezone: "Asia/Shanghai"
          sources:
            knowledge_search: "<knowledge-source-id>"
            metrics_query: "<metrics-source-id>"
            logs_search: "<logs-source-id>"
            trace_get: "<trace-source-id>"
```

仅在显式开启 `allow_loopback_http` 时允许无 TLS 的数字回环地址；其他位置必须使用 HTTPS。服务的 Bearer 入口凭据仅由 Worker 解析。调用时团队、环境、区域、服务、源及查询时间均由 Run 范围与部署白名单共同确定；Run、调用和范围标识由 Worker 注入，模型无法覆盖。服务返回的成功证据必须匹配完整范围、调用、范围标识和时间窗口；不符时拒绝作为证据。`empty`、`denied`、`timeout` 和其他状态分别传播，不会伪装为成功。

本地协议回归直接运行用户的 `workbench-mcp-server` 应用，使用合成上游数据，覆盖四个工具、身份、范围、SQLite 预算与证据；不连接真实企业数据源。运行该服务仍需独立配置其上游和范围授权，并使用其 README 中的说明启动。特别是知识 V2 的独占数据源隔离必须由部署方实际验证，不能仅因为配置了 `scope_enforced` 就推断隔离已成立。缺少真实上游时，企业业务验收仍未完成。

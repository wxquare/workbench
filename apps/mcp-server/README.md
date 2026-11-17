---
title: "Workbench MCP Server：运行与接入"
---

## 能力与状态

独立 Python 3.12 MCP 服务，向 DoD Agent 提供知识、指标、日志与 Trace 四种只读查询。支持 stdio 与 Streamable HTTP，不依赖聊天中的工具连接。

已实现本地可验证的运行时、认证、完整资源元组授权、HTTP 连接器、证据、脱敏、分页与资源限制。**尚未连接真实业务系统**：监控、日志与 Trace 的真实接入需要提供符合下述协议的受控网关，或按真实 API 实现新的适配器。不能把 Prometheus/日志平台 URL 直接填成规范化接口。

## 安装与验证

```bash
uv sync --frozen --python 3.12
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
uv run python -m build --no-isolation
```

`uv.lock` 固定可复现依赖；官方 MCP SDK 固定为 1.30.0。cryptography 采用 46.x，以使用当前 macOS 支持的预编译 wheel。

测试只访问本地回环服务器，使用固定样例，不需要公司账号。安装 wheel 后也可从源码目录外运行 `workbench-mcp-server`。

## 配置和身份

从项目根目录复制 `config/local/mcp-server.example.yaml` 到 `config/local/mcp-server.yaml`，再填写自己的部署配置。Compose 环境使用 `config/local/mcp-server.compose.example.yaml` 作为模板，并按代理转发的 Host 修改 `allowed_hosts`。示例地址是 `.invalid` 域名，不可用于实际查询；`scope_enforced: false` 会使配置检查失败，需由部署方验证下游隔离后显式改为 true。

部署前填写：

- 四类数据源的真实 endpoint、adapter、binding_id、资源元组，以及只读出站凭据的环境变量引用。
- principal 的入站凭据引用、工具列表和完整 `(team, environment, region, service, source_id)` 白名单。
- 指标模板标识和参数白名单；上游网关负责安全地生成实际查询。
- 日志分页签名密钥（至少 32 字节），批准的知识链接主机和业务敏感字段。
- HTTP 入口的 host/port/allowed_hosts；跨主机访问经受信 TLS 反向代理，不应直接暴露明文监听端口。

真实凭据由运行进程的环境注入，配置仅使用 `env://NAME`。首版不支持 `secret://`。入站 token 与出站 token 分开，不能自动继承桌面聊天中的登录状态。权限配置是启动快照，撤权需要阻断/轮换凭据并重启实例。

```bash
uv run workbench-mcp-server check --config ../../config/local/mcp-server.yaml
uv run workbench-mcp-server serve --config ../../config/local/mcp-server.yaml --transport stdio
uv run workbench-mcp-server serve --config ../../config/local/mcp-server.yaml --transport streamable-http
```

也可通过 `WORKBENCH_MCP_CONFIG` 指定配置路径。`check` 只验证配置、策略和 secret_ref 可解析，不访问下游；输出分别标注 discovery/business_query 未检查。日志写 stderr，stdio stdout 专用于 MCP。

HTTP 端点为 `/mcp`，每个请求必须携带配置的 `Authorization: Bearer ...`。只支持服务身份，不提供交互式 OAuth。HTTP 无状态，不用 session ID 授权；correlation context 不授予任何权限。stdio 身份由 `server.stdio_principal` 固定。

### 知识 V2 的隔离前提

`knowledge-v2` 是具名协议转换器。它把 supporting_knowledge 转换为 matches，并保留来源、guidance、hypotheses、release_id 与受限 scope_filter。

该转换器要求 `isolation: exclusive-source`，且只绑定一个完整资源元组。部署方必须在实际端点、数据集合和凭据层验证独占数据范围，再声明 `scope_enforced: true`；请求参数不能代替访问控制。共享数据源应通过能够强制执行范围授权的规范化网关接入。

## 工具调用

所有工具共用两个必填对象：

```json
{
  "scope": {"team":"ExampleTeam","environment":"test","region":"id","service":"checkout","source_id":"demo-knowledge"},
  "context": {"run_id":"run-1","invocation_id":"call-1","query_scope_ref":"scope-1"}
}
```

`context` 只用于审计关联。日志分页绑定 principal、scope、查询条件和时间窗，5 分钟过期；上下文标识不同不影响同一授权查询的分页。

| 工具 | 额外输入 | 主要结果 |
| --- | --- | --- |
| knowledge_search | query、scene=qa/alert、limit=1..10（默认 3） | matches、guidance、hypotheses、release_id |
| metrics_query | template_id、parameters、start、end、step_seconds=15..3600 | series、missing |
| logs_search | start、end、levels、keyword、trace_id、limit=1..200、cursor | entries、next_cursor、truncated |
| trace_get | trace_id、start、end | trace_id、spans、omitted_spans、truncated |

时间采用显式带时区 RFC 3339，最长一小时。指标和日志的窗口为 `[start,end)`；Trace span 必须落在请求窗口内，结束允许等于 end。没有 live 默认值，也不允许 region=all 或任意 datasource URL、PQL、PromQL。

## Resource 与 Prompt

服务提供一个通用只读诊断指南 Resource 和一个故障排查 Prompt：

| 能力 | 名称或 URI | 用途 |
| --- | --- | --- |
| Resource | `dod://guides/incident-diagnosis` | 通过 `resources/list` 发现、`resources/read` 读取 Markdown 指南，了解调查步骤和现有工具边界 |
| Prompt | `investigate_incident` | 通过 `prompts/list` 发现；向 `prompts/get` 提供 `service`、`environment`、`start`、`end`，生成调查任务和证据要求 |

例如，客户端建立连接并初始化后，可以使用官方 MCP SDK：

```python
from pydantic import AnyUrl

guide = await client.read_resource(AnyUrl("dod://guides/incident-diagnosis"))
prompt = await client.get_prompt(
    "investigate_incident",
    {
        "service": "checkout",
        "environment": "test",
        "start": "2026-11-13T00:00:00Z",
        "end": "2026-11-13T00:10:00Z",
    },
)
# guide.contents 是指南内容；prompt.messages 是供客户端使用的任务消息。
```

指南是通用示例，不包含真实业务 Runbook 或实时数据。模板只生成消息，实际查询由客户端或 Agent 决定并调用工具；调用前仍须补全团队、区域、各工具的 `source_id`、审计 context 和其他必需参数。HTTP 读取与获取模板沿用 Bearer 认证，stdio 沿用配置的固定身份；模板不授予工具或数据访问权限。

## 规范化 HTTP 网关协议 v1

`normalized-http-v1` 使用 POST 到配置的精确 endpoint，附加该数据源的出站 Bearer 凭据。请求示例：

```json
{
  "schema_version":"normalized-query-v1",
  "tool":"metrics_query",
  "scope":{"team":"ExampleTeam","environment":"test","region":"id","service":"checkout","source_id":"demo-metrics"},
  "parameters":{"template_id":"error_rate","parameters":{"route":"purchase"},"start":"2026-11-13T00:00:00+00:00","end":"2026-11-13T00:10:00+00:00","step_seconds":60}
}
```

上游必须强制隔离 scope，并按具名模板构造安全查询，不能忽略 scope 或把调用者参数直接插入查询语言。本服务仅接受管理员允许的 template_id、参数名、类型与枚举值。

HTTP 200 响应直接是对应 data 对象，不包 `status/data` 外壳。成功零命中也必须包含对应空数组。各类最小示例：

```json
{"matches":[{"source_id":"doc-7","title":"Guide","content":"Check error rate","source_uri":null,"updated_at":null}]}
```

```json
{"series":[{"labels":{"service":"checkout"},"points":[["2026-11-13T00:01:00Z",0.12]],"unit":"ratio","source_id":"error-rate"}]}
```

```json
{"entries":[{"timestamp":"2026-11-13T00:01:00Z","service":"checkout","level":"ERROR","message":"Request failed","record_id":"log-7"}],"next_cursor":null}
```

```json
{"trace_id":"trace-7","spans":[{"span_id":"span-7","parent_id":null,"service":"checkout","name":"purchase","error":true,"start":"2026-11-13T00:01:00Z","end":"2026-11-13T00:01:01Z"}]}
```

具体严格 Schema 见 `infrastructure/connectors/schemas.py`；未允许字段被拒绝。知识嵌套 guidance/hypotheses 有明确字段白名单，不能透传任意内部对象。Trace 身份、span 时间和日志服务/时间必须匹配请求。范围外 Trace span 会被省略，其父引用被清除并标注完整性限制；上游自身仍必须满足读取隔离要求。

不跟随重定向，不自动重试。401/403 表示该出站绑定不可用；429/502/503/504 建议 Agent 在预算内重试。网络、超时和非法响应有独立错误码，不暴露原始响应或异常。

## 结果与证据

MCP 同时返回 structuredContent 和兼容 JSON 文本。格式为 `dod-tool-result-v1`，包含 status、data、evidence_refs、artifacts、error、retryable、meta。status 为 ok/empty/unavailable/denied/invalid_input/timeout/unknown。empty 只表示成功查询没有命中，不能解释为没有异常。

Evidence 携带绑定、调用、范围版本、完整资源元组、查询时间窗、观察时间、来源 locator 和质量。没有获准链接时 source_uri 为 null，不伪造 URL。链接只保留批准的 HTTPS 主机，并移除查询参数；首版 artifacts 为空。

默认单调用总 deadline 20 秒，实例最多 4 个查询，每个主体最多 2 个；过载直接返回，不无限排队。上游响应上限 2 MiB，MCP 两份序列化结果合计上限 256 KiB。日志单条摘要上限 2048 字符，指标最多 100 series／10000 点，Trace 最多 500 span。所有截断均标注；若本地丢弃日志记录，清除 next_cursor，避免继续分页时悄悄跳过记录，需缩小时间窗/limit 重查。

认证字段和业务配置字段做脱敏；配置中的真实 token 同时被屏蔽。脱敏不代替源端的数据最小化或完整敏感数据分类，新增业务字段应显式配置。审计不保存查询原文、日志正文、header 或原始响应。HTTP 断连与 MCP 取消会停止本地在途操作，但不保证远端查询已终止。

## DoD Agent 接入

Agent 的 MCP 客户端按运行进程配置连接，不读取聊天工具状态。stdio 客户端配置的核心字段：

```json
{
  "command":"/absolute/path/to/.venv/bin/workbench-mcp-server",
  "args":["serve","--config","/absolute/path/to/dod/config/local/mcp-server.yaml","--transport","stdio"]
}
```

子进程环境由部署器注入对应 secret_ref 的变量。HTTP 客户端配置 endpoint `/mcp` 和入站 token 引用，工具绑定映射到上述四个名称、各自 Schema、范围和 deadline；禁止把 token 放进工具参数。可用官方 SDK `ClientSession` 的 initialize、list_tools、call_tool 验证，集成测试提供可运行样例。

Agent 已实现 `workbench_agent.tools.mcp.MCPAdapter` 和 `PolicyToolExecutor`，当前装配的客户端使用 Streamable HTTP；Server 同时支持 stdio。Agent 与本服务的本地合成链路已有测试，真实企业数据源仍需独立接入和验收。

## DDD 与 TDD

领域层只依赖标准库，承载 QueryScope、TimeWindow、Principal、AccessPolicy 和 Evidence；应用层定义查询、结果与出站端口；基础设施实现 HTTP、认证、配置、审计和游标；接口层适配 MCP，bootstrap 装配依赖。

四类查询共用经过验证的授权/执行边界，以 QueryRequest 的具名工具区分；不额外创建只有转发作用的四个服务类。CLI 是进程入口，允许配置加载和调用组合根；其余接口不得直连 connector。架构测试验证这些依赖规则。

按行为先写失败测试，再实现，再重构；测试分为 unit、contract、integration、acceptance。真实外部验收需单独完成，见 [MCP 验收记录](../../docs/mcp-server/acceptance.md)。

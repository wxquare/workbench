---
title: "Workbench MCP Server：实现与验收记录"
date: "2026-11-13"
status: offline-verified
---

## 验证结果

| 项目 | 结果 | 证据 |
| --- | --- | --- |
| 自动化测试 | 71 passed | Python 3.12.3；pytest -q；包含 unit、contract、integration、acceptance |
| 静态检查 | 通过 | ruff check src tests |
| 格式检查 | 通过 | ruff format --check src tests |
| 依赖安装 | 通过 | uv lock、uv sync --frozen；官方 MCP SDK 1.30.0 |
| 包构建 | 通过 | python -m build --no-isolation，生成 wheel 与 sdist |
| 安装后运行 | 通过 | wheel 安装到独立虚拟环境，从源码外执行 check、MCP initialize、发现四工具并查询固定证据 |
| stdio | 通过 | 官方 ClientSession + 服务子进程 + 本地受控 HTTP 数据源 |
| Streamable HTTP | 通过 | 官方 ClientSession + 本地 uvicorn + Bearer 认证，非法 token 返回 401 |
| 断连取消 | 通过 | SDK 无状态 transport 正在执行时送入 http.disconnect，网关操作被取消 |
| 真实业务系统 | 未验收 | 未配置或调用真实知识、监控、日志、Trace 数据源 |
| dod-agent 全链路 | 未验收 | 本任务未修改 Agent 运行时，提供独立 MCP 接入接口与示例 |

## TDD 与行为覆盖

先运行测试确认缺失能力或错误行为导致失败，再实现最小逻辑并回归，最后重构。主要 Red/Green 批次：

1. 领域／用例／配置：22 项由缺少实现失败至通过，覆盖完整元组授权、时间窗、拒绝零查询、empty/unavailable/timeout 区分、审计与取消。
2. HTTP 连接器：13 项由缺少连接器失败至通过，覆盖真实请求编解码、出站凭据、一次尝试、重定向拒绝、响应上限和来源映射。
3. MCP／输出／游标：5 项由缺少实现失败至通过，覆盖两个 transport、outputSchema、四工具发现、脱敏、字节预算和签名范围。
4. 审查回归：先复现并确认失败，再修正引号 JSON 日志、完整认证头、Cookie、断连、分页跳记录、知识嵌套白名单、显式知识源隔离、非法 endpoint、自定义字段覆盖默认脱敏、非法 YAML 泄漏以及不完整知识响应误判为空。

测试使用真实领域模型、执行器、HTTP 客户端和 MCP SDK；仅外部业务系统替换为本地受控 HTTP 服务。架构测试验证绝对/相对导入依赖方向；CLI 作为进程入口允许装配依赖。

独立代码审查提出的问题均已修复并形成回归测试。未使用原始业务日志或真实凭据生成测试样例。

## 真实接入验收清单

部署管理员在配置文件和密钥环境准备好后，逐项记录实际环境的结果。测试数据不能替代此项。

- [ ] 服务入口地址、TLS、调用 principal、工具白名单和完整资源元组已登记。
- [ ] 每个 source 的 endpoint、协议、凭据引用及上游读取隔离已验证，再启用 scope_enforced。
- [ ] DP 知识 V2 使用独占源且仅绑定一个元组；共享源已通过强制隔离网关接入。
- [ ] 指标网关按模板与 scope 查询，日志与 Trace 支持本项目严格响应 Schema。
- [ ] check 仅表示配置通过；MCP initialize/tools/list 单独验证。
- [ ] 知识题引用已知文档 source_id，指导与运行结论分开。
- [ ] 固定窗口指标与原平台数值、时间、单位一致。
- [ ] 日志记录 ID、范围和脱敏结果与原平台一致。
- [ ] 已知 Trace 的 trace_id、span 时间、错误与授权服务范围一致。
- [ ] 跨团队／live／其他服务查询被拒绝，未发生出站查询。
- [ ] dod-agent 配置 MCPAdapter/ToolBinding 后完成实际 Run 验收。

## 交付边界

首版 normalized-http-v1 是明确的网关协议，不是现有监控／日志平台的通用兼容层。没有这样的网关时，需要按照真实 API 增加具名连接器；README 已给出请求和四种响应示例。

config/example.yaml 故意使用不可解析的示例域名，并默认不声明上游已隔离。复制并完成部署配置后才能通过 check；实际 token 仅通过环境变量注入。没有自动启动生产服务、发布、通知或创建工单。

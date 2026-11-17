---
title: Workbench（我的工作台）：可配置 Agent 与 MCP 服务
---

“我的工作台”承载多个独立可配置 Agent，DoD Agent 是其中面向值班答疑、诊断和跟进的场景。Agent 直接拥有配置、任务、会话、资源和授权，不再设置 Workspace 产品或领域层；LangChain 专家是工作流内部角色，Runner 的 `/workspace` 仅是技术运行目录。

Workbench 将 Python Agent 服务、Vue 工作台和只读 MCP 服务统一放在 `apps/`。Agent 服务提供 API、Worker 与 Scheduler；MCP Server 独立进程运行。三个组件各自有锁文件和测试。

- `apps/agent`：Python 3.12 Agent，包名 `workbench_agent`，命令 `workbench`。
- `apps/web`：Vue 3、TypeScript、Vite、Element Plus 工作台；任务记录、Agent 管理、模型配置和会话已接业务 API，其余管理页面仍有占位内容。
- `apps/mcp-server`：Python 3.12 MCP 服务，包名 `workbench_mcp_server`，命令 `workbench-mcp-server`。
- `config/`：Agent 与 MCP 配置示例、本地配置和工作流。实际本地配置不纳入 Git。
- `deploy/`：两个镜像构建文件与本地 Compose；需在装有 Docker 的环境验证。
- `docs/`：规格、图、验收、历史计划和运行手册。

Python 环境要求 3.12，Agent 所链接的 SQLite 版本至少为 3.51.3；Web 使用 Node.js 22 和 npm。安装、初始化、启动和测试命令见 [源码结构与本地启动](docs/runbooks/source-layout.md)。

首次克隆后，将 `config/local/agent.example.yaml` 复制为 `config/local/agent.yaml`，再按环境配置；后者由 Git 忽略。MCP 配置也从 `config/local/mcp-server.example.yaml` 复制为被忽略的 `config/local/mcp-server.yaml`。实际密钥只由运行环境注入。

更名后从新目录启动服务，并将原 `DOD_*` 环境变量改为 `WORKBENCH_*`；使用 OIDC 时还需同步将 audience 配置为 `workbench-agents`。现有 `var/dod.sqlite3` 及其 WAL/SHM 文件随项目原样保留，不会自动迁移。`dod.v*` 工作流版本、`dod-storage-agent-v1`、`dod-checkpoint-v1` 和 `dod-mcp-v1` 是已有数据或业务协议标识，继续保持不变。Compose 数据卷已改名为 `workbench-data`，使用旧卷的部署需显式迁移数据。

Agent 领域重构采用 `agents`、`agent_id`、`agent_scope`、`agent_profile`、`/v1/agents` 和 `/app/agents`，实现与验证边界见 [主规格 21.4](docs/specs/2026-11-12-workbench-python-spec.md#214-移除-workspace-的-agent-领域重构)。新 Schema 只在显式选择的新 SQLite 路径初始化；旧 Workspace 库必须安全拒绝迁移和 readiness，并保留原文件及数据，不能由代码或启动过程删除。

当前新基线为 `schema_agent_v1`、独立迁移 `0001_agent_domain` 和 `dod-storage-agent-v1`；旧迁移与快照仅供历史参考。新建 Agent 使用团队级模型和独立草稿默认值，不继承默认 Agent 的系统提示词或其他配置；创建不依赖现有活动 Agent，也不会自动授予模型凭据。本次未修改实际运行数据库。

数据库初始化、迁移和 bootstrap 是显式运维步骤；正常运行进程不会自动执行。配置文件仅包含凭据引用，实际密钥由运行环境注入。MCP 示例配置不能直接访问真实业务系统，接入前需要配置和验收数据源及权限。

历史路径 `projects/dod-agent/` 和 `projects/dod-mcp-server/` 已分别迁入 `apps/agent`、`apps/web` 和 `apps/mcp-server`；原有旧目录及其本地文件已于 2026-11-15 移入废纸篓。

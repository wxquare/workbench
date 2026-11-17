---
title: Workbench（我的工作台）：可配置 Agent 与 MCP 服务
---

“我的工作台”承载多个独立可配置 Agent，DoD Agent 是其中面向值班答疑、诊断和跟进的场景。Agent 直接拥有配置、任务、会话、资源和授权；LangChain 专家是工作流内部角色，Runner 的 `/workspace` 是技术运行目录。

Workbench 将 Python Agent 服务、Vue 工作台和只读 MCP 服务统一放在 `apps/`。Agent 服务提供 API、Worker 与 Scheduler；MCP Server 独立进程运行。三个组件各自有锁文件和测试。

- `apps/agent`：Python 3.12 Agent，包名 `workbench_agent`，命令 `workbench`。
- `apps/web`：Vue 3、TypeScript、Vite、Element Plus 工作台；任务记录、Agent 管理、模型配置和会话已接业务 API，其余管理页面仍有占位内容。
- `apps/mcp-server`：Python 3.12 MCP 服务，包名 `workbench_mcp_server`，命令 `workbench-mcp-server`。
- `config/`：Agent 与 MCP 配置示例、本地配置和工作流。实际本地配置不纳入 Git。
- `deploy/`：两个镜像构建文件与本地 Compose；需在装有 Docker 的环境验证。
- `docs/`：规格、架构图、验收和运行手册。

Python 环境要求 3.12，Agent 所链接的 SQLite 版本至少为 3.51.3；Web 使用 Node.js 22 和 npm。安装、初始化、启动和测试命令见 [源码结构与本地启动](docs/runbooks/source-layout.md)。

首次克隆后，将 `config/local/agent.example.yaml` 复制为 `config/local/agent.yaml`，再按环境配置；后者由 Git 忽略。MCP 配置也从 `config/local/mcp-server.example.yaml` 复制为被忽略的 `config/local/mcp-server.yaml`。实际密钥只由运行环境注入。

数据库初始化、迁移和 bootstrap 是显式运维步骤；正常运行进程不会自动执行。配置文件仅包含凭据引用，实际密钥由运行环境注入。MCP 示例配置不能直接访问真实业务系统，接入前需要配置和验收数据源及权限。

# Workbench

可配置 Agent 工作台，包含 Python Agent 服务、Vue 管理界面和只读 MCP 服务。DoD Agent 是其中面向值班答疑、诊断和跟进的场景。每个 Agent 独立管理配置、任务、会话、资源和授权。

## 项目结构

- **Agent 服务** [`apps/agent`](apps/agent/)：Python 3.12；提供 API、Worker 和 Scheduler。包名 `workbench_agent`，命令 `workbench`。
- **Web 工作台** [`apps/web`](apps/web/)：Vue 3、TypeScript、Vite 和 Element Plus；提供任务、Agent、模型和会话管理界面。
- **MCP 服务** [`apps/mcp-server`](apps/mcp-server/)：独立运行的只读服务。包名 `workbench_mcp_server`，命令 `workbench-mcp-server`。
- **配置与部署** [`config`](config/) · [`deploy`](deploy/)：配置示例、工作流、镜像构建文件和本地 Compose。
- **文档** [`docs`](docs/)：规格、架构图、验收记录和运行手册。

## 本地开始

需要 Python 3.12、SQLite 3.51.3 或更高版本，以及 Node.js 22 和 npm。从仓库根目录复制配置示例：

```bash
cp config/local/agent.example.yaml config/local/agent.yaml
cp config/local/mcp-server.example.yaml config/local/mcp-server.yaml
```

本地配置已被 Git 忽略；实际密钥由运行环境注入。MCP 示例配置不能直接访问真实业务系统，接入前需要配置并验收数据源与权限。

安装、数据库初始化、启动和测试步骤见 [本地安装与启动](docs/runbooks/source-layout.md)。数据库迁移与 bootstrap 需要显式执行，正常运行进程不会自动执行。

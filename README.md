# Workbench

可配置 Agent 工作台，包含 Python Agent 服务、Vue 管理界面和只读 MCP 服务。DoD Agent 是其中面向值班答疑、诊断和跟进的场景。每个 Agent 独立管理配置、任务、会话、资源和授权。

## 项目结构

- **Agent 服务** [`apps/agent`](apps/agent/)：Python 3.12；提供 API、Worker 和 Scheduler。包名 `workbench_agent`，命令 `workbench`。
- **Web 工作台** [`apps/web`](apps/web/)：Vue 3、TypeScript、Vite 和 Element Plus；提供任务、Agent、模型和会话管理界面。
- **MCP 服务** [`apps/mcp-server`](apps/mcp-server/)：独立运行的只读服务。包名 `workbench_mcp_server`，命令 `workbench-mcp-server`。
- **配置与部署** [`config`](config/) · [`deploy`](deploy/)：配置示例、工作流、镜像构建文件和本地 Compose。
- **本地脚本** [`scripts`](scripts/)：安装、显式初始化数据库和启动服务。
- **文档** [`docs`](docs/)：规格、架构图、验收记录和运行手册。

## 本地开始

需要 Python 3.12、SQLite 3.51.3 或更高版本，以及 Node.js 22、npm 和 uv。从仓库根目录安装依赖并构建 Web 界面：

```bash
./scripts/install.sh
```

安装脚本仅在本地配置不存在时复制示例，不会覆盖已有配置。按环境设置 `WORKBENCH_PERSISTENT_ROOT`、`WORKBENCH_DATABASE_URL` 和所需密钥后，首次使用**新数据库路径**时显式运行 `./scripts/init-db.sh`；日常运行使用 `./scripts/start.sh`。配置好 MCP 数据源与权限后，可用 `./scripts/start.sh --mcp` 一起启动 MCP 服务。

本地配置已被 Git 忽略，实际密钥由运行环境注入。数据库迁移与 bootstrap 不会在日常启动时自动执行。环境变量示例、手动命令和测试步骤见 [本地安装与启动](docs/runbooks/source-layout.md)。

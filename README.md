# Workbench

可配置 Agent 工作台，包含 Python Agent 服务、Vue 管理界面和只读 MCP 服务。DoD Agent 是其中面向值班答疑、诊断和跟进的场景。每个 Agent 独立管理配置、任务、会话、资源和授权。

## 项目结构

- **Agent 服务** [`apps/agent`](apps/agent/)：Python 3.12；提供 API、Worker 和 Scheduler。包名 `workbench_agent`，命令 `workbench`。
- **Web 工作台** [`apps/web`](apps/web/)：Vue 3、TypeScript、Vite 和 Element Plus；提供任务、Agent、模型和会话管理界面。
- **MCP 服务** [`apps/mcp-server`](apps/mcp-server/)：独立运行的只读服务。包名 `workbench_mcp_server`，命令 `workbench-mcp-server`。
- **配置与部署** [`config`](config/) · [`deploy`](deploy/)：配置示例、工作流、镜像构建文件和本地 Compose。
- **本地脚本** [`scripts`](scripts/)：安装、显式初始化数据库和启动服务。
- **文档** [`docs`](docs/)：产品、架构、开发、运维、决策与参考手册；入口见 [docs/README.md](docs/README.md)。

## 本地开始

需要 Python 3.12、SQLite 3.51.3 或更高版本，以及 Node.js 22、npm 和 uv。克隆仓库后，在项目根目录执行：

```bash
./scripts/install.sh
```

安装后、初始化前，先检查 `config/local/agent.yaml` 并按需注入模型等凭据。数据默认存放在 `$HOME/.workbench`；安装时可用 `./scripts/install.sh --data-dir /absolute/path` 选择其他目录。选择记录在被 Git 忽略的本地文件中，重新安装不会覆盖已有配置或移动数据。

```bash
./scripts/init-db.sh
./scripts/start.sh
```

`init-db.sh` 仅用于**新数据库**，不会覆盖现有文件；已有且兼容的数据库直接运行 `start.sh`。日常启动不会自动迁移或 bootstrap。配置好 MCP 数据源与权限后，可用 `./scripts/start.sh --mcp` 一起启动 MCP 服务。实际密钥仍由运行环境注入；路径覆盖、手动命令和测试步骤见 [运维手册](docs/04-operations.md)。

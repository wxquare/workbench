---
title: 我的工作台源码结构：本地安装与启动
date: 2026-11-14
---

## 安装与构建

在项目根目录执行，`WORKBENCH_ROOT` 是绝对路径；在每个新终端重新设置这些变量。本文采用 Agent 领域新契约，实现与验证边界详见 [主规格 21.4](../specs/2026-11-12-workbench-python-spec.md#214-移除-workspace-的-agent-领域重构)。

本机可先从项目根目录运行 `./scripts/install.sh`。脚本要求已安装 `uv` 和 `npm`，安装两个 Python 组件的开发依赖、安装 Web 依赖并构建页面；仅在本地 Agent/MCP 配置不存在时复制示例，不覆盖已有配置。下面是对应的手动命令。

```bash
export WORKBENCH_ROOT="$PWD"
export WORKBENCH_ASSET_ROOT="$WORKBENCH_ROOT"
export WORKBENCH_FRONTEND_DIST_ROOT="$WORKBENCH_ROOT/apps/web/dist"

cd "$WORKBENCH_ROOT/apps/agent"
uv sync --frozen --group dev
uv run workbench --help

cd "$WORKBENCH_ROOT/apps/web"
npm ci
npm run build
# UI 独立预览：npm run dev，打开终端显示的 /app/ 地址

cd "$WORKBENCH_ROOT/apps/mcp-server"
uv sync --frozen --group dev
uv run workbench-mcp-server --help
```

`WORKBENCH_FRONTEND_DIST_ROOT` 必须是绝对路径，缺省时沿用 `WORKBENCH_ASSET_ROOT/web/dist`。构建完成后 Agent API 在 `/app/` 托管页面；未构建时返回 503。

## Agent 本地启动

当前 Agent Schema 使用 `schema_agent_v3.py`、独立 Alembic 目录 `persistence/agent_migrations/`、revision `0003_run_trace_contexts`、`schema_version=dod-storage-agent-v1` 和 `checkpoint_codec_version=dod-checkpoint-v1`。已有兼容 Agent 数据库升级前需停止服务并备份，再显式运行迁移；详见 [可观测性升级手册](observability.md)。旧 Workspace 库不能沿此链升级。源码更新不会自动修改实际运行数据库，也不会自动部署到现有进程。

`./scripts/install.sh` 会在缺少本地配置时复制 `config/local/agent.example.yaml` 为被 Git 忽略的 `config/local/agent.yaml`；手动安装时可执行下列命令。按部署环境配置其中的数据库、模型、存储和认证引用，按需添加模型凭据授权绑定；实际密钥只由运行环境注入。设置 `WORKBENCH_PERSISTENT_ROOT` 与 `WORKBENCH_DATABASE_URL` 为绝对的本地持久路径，按需提供对应凭据。SQLite 最低版本 3.51.3；配置根路径不可依赖当前工作目录。

```bash
cp -n "$WORKBENCH_ROOT/config/local/agent.example.yaml" "$WORKBENCH_ROOT/config/local/agent.yaml"
```

首次启用 Agent 新 Schema 时，必须显式选择尚不存在的新数据库文件。下面路径仅为示例，须替换为本机持久磁盘上的绝对路径；若文件已存在，先确认它是同一版本的 Agent 数据库，不能覆盖或删除。API、Worker、Scheduler、迁移和 doctor 必须使用同一路径、持久根及配置。

```bash
export WORKBENCH_PERSISTENT_ROOT=/absolute/local/persistent/workbench
export WORKBENCH_DATABASE_URL=sqlite+aiosqlite:////absolute/local/persistent/workbench/agents-20261117.sqlite3
```

首次使用**尚不存在**的数据库文件时，从项目根目录运行 `./scripts/init-db.sh`。脚本拒绝已有文件，并依次执行迁移、bootstrap 和 doctor；已有数据库的升级仍按维护手册手动操作。对应的手动命令需从 `apps/agent` 执行：

```bash
uv run workbench db migrate --config "$WORKBENCH_ROOT/config/local/agent.yaml"
uv run workbench bootstrap --config "$WORKBENCH_ROOT/config/local/agent.yaml"
uv run workbench doctor --config "$WORKBENCH_ROOT/config/local/agent.yaml"
```

旧 Workspace 库必须在迁移、doctor 和 readiness 阶段安全拒绝，并提示选择新路径及显式初始化。保留旧库、WAL/SHM 和备份；不得通过清表、删库、手动 stamp 或自动重置绕过拒绝。新库需重新建立成员、Agent、模型与资源绑定，以及部署凭据授权；旧任务不会自动导入。普通启动不迁移、不 bootstrap，也不删除或自动切换数据库。

已有且兼容的 Agent 数据库日常可从项目根目录运行 `./scripts/start.sh`。脚本先执行 doctor，再启动 API、Worker 和 Scheduler；任一进程退出或按 Ctrl-C 时会停止其余进程，不会自动迁移或 bootstrap。也可在确认 doctor 就绪后，在三个终端分别从 `apps/agent` 手动启动，并确保环境变量与配置一致：

```bash
uv run workbench api --config "$WORKBENCH_ROOT/config/local/agent.yaml" --host 127.0.0.1 --port 8000
uv run workbench worker --config "$WORKBENCH_ROOT/config/local/agent.yaml"
uv run workbench scheduler --config "$WORKBENCH_ROOT/config/local/agent.yaml"
```

API 页面为 `http://127.0.0.1:8000/app/`，Agent 管理入口为 `/app/agents`，API 为 `/v1/agents`，健康入口为 `/health/live` 和 `/health/ready`。任务记录、Agent 管理、模型配置和会话已接业务 API；其余管理页面仍有占位内容，页面存在不能证明真实数据源已经接通。直接问答配置见 [纯 LLM 问答](../../apps/agent/DIRECT_LLM_QA_SETUP.md)，数据库备份与恢复见 [SQLite 运行手册](sqlite-recovery.md)。

部署配置中的单资源归属使用 `agent_id`，连接器和模型凭据白名单使用 `agent_ids`，身份限制使用 `agent_scope`。模型、Skill 等允许团队共享的资源保留明确的可空 Agent 作用域。旧 `workspace_id`/`workspace_ids` 配置须显式改写，不能静默忽略。Runner 容器内 `/workspace` 是技术运行目录，无需因产品领域改名而更改该路径。

新建 Agent 的初始模型来自团队级已发布模型，其他字段使用新草稿的领域默认值，不继承现有默认 Agent 的系统提示词、行为、上下文、预算或资源绑定。先准备团队级模型，再独立配置并发布各 Agent；所有模型凭据均须配置部署授权，管理端密钥还需使用下述加密存储。默认 `dod.v3` 仅记录输入；直接问答需选择 `dod.v17` 或 `dod.v18`。

### 管理端模型密钥

管理端可为已保存的模型输入 API Key。服务端使用 Fernet 加密后保存在 `$WORKBENCH_PERSISTENT_ROOT/model-secrets/`，文件权限为 `0600`、目录权限为 `0700`；数据库和接口只保存、返回 `secret://managed-model/...` 引用。保存操作记入审计，不记录密钥值。旧 `env://...` 凭据和部署白名单继续可用。

首次启用时，先在可信机器上一次性生成加密主密钥：

```bash
mkdir -p "$HOME/.config/my-workbench"
chmod 700 "$HOME/.config/my-workbench"
cd "$WORKBENCH_ROOT/apps/agent"
uv run python -c 'import os; from cryptography.fernet import Fernet; p=os.path.expanduser("~/.config/my-workbench/model-master.key"); f=os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600); os.write(f, Fernet.generate_key()); os.close(f)'
```

命令不会覆盖已有主密钥。API 和 Worker 启动前分别设置相同的 `WORKBENCH_PERSISTENT_ROOT` 和主密钥，然后重启这两个进程：

```bash
export WORKBENCH_MODEL_SECRET_MASTER_KEY="$(<"$HOME/.config/my-workbench/model-master.key")"
```

模型配置中先保存模型草稿，再输入 API Key 并点击“保存密钥”，随后发布模型版本；到 Agent 管理中选择新模型版本并发布 Agent，新的 Run 才使用新版本。旧 Run 仍使用固定版本；不要删除旧凭据文件。团队共享模型的密钥可供同团队且经部署白名单授权使用该地址的 Agent 使用，Agent 专属模型的密钥仅限该 Agent。`service.model_credential_bindings` 中必须存在匹配的团队、Agent 和地址；管理端密钥不要求引用与该条目的环境变量引用相同。部署配置 `service.managed_model_endpoints` 控制可使用管理端凭据的 HTTPS 地址，默认仅允许 `https://api.deepseek.com`；配置其他地址前须确认其可信。

主密钥文件与密文目录需分别安全备份并限制访问；丢失或更换主密钥会使旧凭据无法解密。目录和文件须由 API、Worker 所运行的同一系统用户持有；多机部署需安全共享密文目录及主密钥，不能使用各机不同的密钥。生产环境应使用 HTTPS、OIDC 和受控密钥分发；该本地文件方案不代替集中式密钥管理服务。保存密钥失败时，不会自动切换至环境变量凭据。Worker 启用该存储时，即使 `content_mode` 设为 `redacted_content`，也不会导出模型输入和输出正文，避免托管密钥泄露。

仓库、上传和执行资源代码位于 `workbench_agent.execution`；执行适配为 `workbench_agent.tools.execution_executor.ExecutionExecutor` 与 `ExecutionBinding`。这些技术模块与产品 Agent 身份分开，Runner 的路径隔离和清理契约保持有效。

## MCP 服务

`./scripts/install.sh` 会在缺少本地 MCP 配置时复制示例。按需填写服务身份、源、隔离声明、端口与环境变量引用；示例 URL 是占位值。环境实际注入所需 token 后，可从项目根目录运行 `./scripts/start.sh --mcp`，在 Agent 服务之外启动 MCP；脚本会先检查 MCP 配置。单独启动时在 `apps/mcp-server` 执行：

```bash
uv run workbench-mcp-server check --config "$WORKBENCH_ROOT/config/local/mcp-server.yaml"
uv run workbench-mcp-server serve --config "$WORKBENCH_ROOT/config/local/mcp-server.yaml" --transport streamable-http
```

Server 支持 stdio 与 Streamable HTTP；Agent 当前装配的客户端使用 Streamable HTTP。非回环服务连接必须使用受信 HTTPS 入口。详细配置和数据源条件见 [MCP 运行说明](../../apps/mcp-server/README.md)。

## 测试与容器

```bash
python3 -m unittest discover -s scripts/tests -v
cd "$WORKBENCH_ROOT/apps/agent" && uv run python -m pytest
cd "$WORKBENCH_ROOT/apps/mcp-server" && uv run python -m pytest && uv run ruff check . && uv run ruff format --check .
cd "$WORKBENCH_ROOT/apps/web" && npm test && npm run typecheck && npm run build
cd "$WORKBENCH_ROOT"
docker compose -f deploy/compose.local.yml config
docker build -f deploy/Dockerfile.agent -t workbench-agent:local .
docker build -f deploy/Dockerfile.mcp-server -t workbench-mcp-server:local .
```

Compose 的 API 只绑定宿主机 `127.0.0.1:8000`；API、Worker 和 Scheduler 共用 `workbench-data` 卷，先显式执行迁移和 bootstrap，再启动服务。MCP profile 使用 `config/local/mcp-server.yaml`。在容器环境请先复制 `config/local/mcp-server.compose.example.yaml` 为该文件；此示例监听容器内 `0.0.0.0:8001`，并以 `mcp-server:8001` 作为内部 Host。配置真实测试源与身份，并按可信 TLS 代理实际转发的 Host 调整 `allowed_hosts`；服务不映射宿主机端口。Agent 不会自动通过明文内部地址连接服务。以上容器命令需要在安装 Docker 的环境运行和验收。

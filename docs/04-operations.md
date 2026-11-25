---
title: 运维手册
status: stable
verified_commit: 99df50d
---

# 运维手册

> **读者**：运维、值班、需要在本机或容器里把系统跑起来的人。
> **只写可执行的动作**：命令、前置条件、失败后的处置。设计原因见 [02 架构](02-architecture.md)，代码入口见 [03 开发](03-development.md)。
> **通用原则**：停写 → 备份 → 显式维护 → 校验就绪，不要跳过任何一步；所有数据库与持久目录使用**绝对路径**，且 API / Worker / Scheduler / 维护命令必须使用同一份配置和同一个库。

**目录**

1. 安装与首次初始化
2. 启动、健康检查与关闭
3. SQLite 备份与恢复
4. Runner 与产物
5. 预算策略
6. 可观测性
7. 动作策略与人工重试
8. 合成接入
9. 托管模型密钥
10. 排障速查

---

## 1 安装与首次初始化

### 1.1 环境要求

| 依赖 | 要求 |
| --- | --- |
| Python | 3.12；用 `uv` 管理，按 `uv.lock` 安装 |
| Node | 用于构建 Web；`npm ci` |
| SQLite | 运行库 ≥ 3.51.3（启动时校验） |
| 磁盘 | 本地持久磁盘；**不要**把 SQLite 放在 NFS 或跨主机共享文件系统 |
| 可选 | Redis 7.2（仅唤醒加速，缺失可降级）、S3 兼容对象存储、Docker 或 Podman（Runner） |

### 1.2 一键安装

从仓库根目录：

```bash
./scripts/install.sh
```

脚本行为：

- 安装 `apps/agent` 与 `apps/mcp-server` 的开发依赖，安装 Web 依赖并构建页面。
- **仅在配置不存在时**复制 `config/local/agent.example.yaml`、`config/local/mcp-server.example.yaml`，不覆盖已有文件。
- 默认持久目录 `$HOME/.workbench`，可用 `--data-dir /absolute/path` 改选；选择记录在被 Git 忽略的 `config/local/data-dir`。
- 重复安装保留已有目录选择；**显式改选不会迁移旧数据**。
- 安装阶段不创建数据库。

等价的拆开命令：

```bash
export WORKBENCH_ROOT="$PWD"
export WORKBENCH_ASSET_ROOT="$WORKBENCH_ROOT"
export WORKBENCH_FRONTEND_DIST_ROOT="$WORKBENCH_ROOT/apps/web/dist"

cd "$WORKBENCH_ROOT/apps/agent"       && uv sync --frozen --group dev
cd "$WORKBENCH_ROOT/apps/web"         && npm ci && npm run build
cd "$WORKBENCH_ROOT/apps/mcp-server"  && uv sync --frozen --group dev
```

`WORKBENCH_FRONTEND_DIST_ROOT` 必须是绝对路径，缺省时取 `WORKBENCH_ASSET_ROOT/web/dist`。未构建时 `/app/` 返回 503。

### 1.3 首次建库

目标数据库文件必须**尚不存在**：

```bash
./scripts/init-db.sh
```

脚本创建数据库父目录、拒绝已有文件，然后依次执行 `db migrate` → `bootstrap` → `doctor`。默认库路径为 `$HOME/.workbench/agents.sqlite3`（自定义数据目录时在其下）。

需要指定路径时：

```bash
export WORKBENCH_PERSISTENT_ROOT=/absolute/local/persistent/workbench
export WORKBENCH_DATABASE_URL=sqlite+aiosqlite:////absolute/local/persistent/workbench/agents.sqlite3
```

`WORKBENCH_PERSISTENT_ROOT` 优先于安装时记录的目录；环境变量只作用于当前终端。

手动分步（仅用于已准备好的数据目录，**不能替代**首次初始化的安全检查）：

```bash
export WORKBENCH_ROOT=/absolute/path/to/workbench
source "$WORKBENCH_ROOT/scripts/lib/local-paths.sh"
workbench_export_storage "$WORKBENCH_ROOT"
export WORKBENCH_ASSET_ROOT="$WORKBENCH_ROOT"
export WORKBENCH_FRONTEND_DIST_ROOT="$WORKBENCH_ROOT/apps/web/dist"
cd "$WORKBENCH_ROOT/apps/agent"
uv run workbench db migrate  --config "$WORKBENCH_ROOT/config/local/agent.yaml"
uv run workbench bootstrap   --config "$WORKBENCH_ROOT/config/local/agent.yaml"
uv run workbench doctor      --config "$WORKBENCH_ROOT/config/local/agent.yaml"
```

### 1.4 旧 Workspace 库的处理

旧 Workspace Schema 的库**不能**沿 Agent 迁移链升级。迁移、`doctor` 和 readiness 会安全拒绝，并提示选择新路径。处理原则：

- 保留旧库、`-wal`/`-shm` 与全部备份，不得删表、删库、手动 stamp 或自动重置。
- 在新路径上显式初始化 Agent 基线，重新建立成员、Agent、模型与资源绑定、部署凭据授权。
- 旧任务不会自动导入；需要保留的历史用旧版本工具在隔离环境读取。

### 1.5 容器

```bash
docker compose -f deploy/compose.local.yml config
docker build -f deploy/Dockerfile.agent      -t workbench-agent:local .
docker build -f deploy/Dockerfile.mcp-server -t workbench-mcp-server:local .
```

- API 只绑定宿主机 `127.0.0.1:8000`；三个进程共用 `workbench-data` 卷。
- 启动前先显式执行维护：`docker compose ... run --rm api db migrate --config /config/agent.yaml`，再 `bootstrap`，最后 `up`。
- 优雅关闭：compose 给 Worker 35 秒、API 20 秒（进程内为 30 秒 / 15 秒）；SIGKILL 无清理，接管方等待租约到期。
- Compose 文件是**配置样例**，不是容器验收结论；需要在安装 Docker 的环境单独验收。

---

## 2 启动、健康检查与关闭

### 2.1 日常启动

```bash
./scripts/start.sh          # 先 doctor，再启动 api / worker / scheduler
./scripts/start.sh --mcp    # 另外启动 MCP Server（先检查 MCP 配置）
```

任一进程退出或按 Ctrl-C 时，脚本停止其余进程。普通启动**不会**迁移、bootstrap 或自动切换数据库。

三个独立终端手动启动（每个终端都要重复 1.3 中的环境变量设置）：

```bash
cd "$WORKBENCH_ROOT/apps/agent"
uv run workbench api       --config "$WORKBENCH_ROOT/config/local/agent.yaml" --host 127.0.0.1 --port 8000
uv run workbench worker    --config "$WORKBENCH_ROOT/config/local/agent.yaml"
uv run workbench scheduler --config "$WORKBENCH_ROOT/config/local/agent.yaml"
```

进程与部署约束（SQLite 首版单机、单 Worker、2 个 Run 并发）见 [02 架构 §2.1](02-architecture.md#21-部署进程)。

### 2.2 入口与健康检查

| 入口 | 地址 |
| --- | --- |
| Web | `http://127.0.0.1:8000/app/` |
| Agent 管理 | `/app/agents` |
| 业务 API | `/v1/agents` |
| 存活 | `/health/live`（只说明进程在） |
| 就绪 | `/health/ready`（依赖与 Schema 判断） |

`doctor` 是一次性就绪检查，也是启动前的门禁：

```bash
uv run workbench doctor --config "$WORKBENCH_ROOT/config/local/agent.yaml"
```

`doctor` 只检查本地存储与 Schema；Redis、连接器、模型推理、对象存储的健康需要在各自环境单独验证，不能由 `doctor` 推断。

### 2.3 升级步骤

1. 停止 API、Worker、Scheduler。
2. 按 [§3](#3-sqlite-备份与恢复) 做一致性备份，并同时备份 Skill 对象目录与暂存目录。
3. 确认配置指向现有 Agent 数据库，而非旧 Workspace 库。
4. 执行维护：

```bash
cd "$WORKBENCH_ROOT/apps/agent"
uv sync --frozen --group dev
uv run workbench db migrate --config "$WORKBENCH_ROOT/config/local/agent.yaml"
uv run workbench doctor      --config "$WORKBENCH_ROOT/config/local/agent.yaml"
```

5. `doctor` 就绪后按 2.1 重新启动。源码更新不会自动修改运行中的数据库，也不会热部署进程。

**迁移被拒绝时不要绕过**。典型拒绝原因与处置：

| 拒绝原因 | 处置 |
| --- | --- |
| 旧 Workspace Schema | 在新路径初始化；保留旧库，见 §1.4 |
| 迁移 `0003_run_timing`：存在已执行的 v2 Run | v2 没有持久区间账本，无法重建首次开始与执行时长。保留备份与原库，组织一次有权威计时证据的离线对账后再升级；不得重置 epoch、猜时间戳或删除 Run |
| 迁移 `0006_usage_ledger`：存在历史用量记录 | 同上，离线核对待用量后再迁移 |
| 迁移 `0005_resume_protocol`：存在歧义的请求/中断别名 | 原子拒绝，需离线审阅；不得手工 stamp head revision |

迁移在单个 `BEGIN IMMEDIATE` 事务中执行；失败保持原 revision、列与行不变。

---

## 3 SQLite 备份与恢复

### 3.1 运行边界

- 单主机、本地持久磁盘、一个绝对数据库路径、一个 Worker（2 个执行槽）。
- WAL / `synchronous=FULL` / 外键开启；存储超时 5 秒，租约 45 秒、每 10 秒续期，恢复扫描每 5 秒。
- **不要**在 NFS 上放 SQLite，**不要**起第二个 Worker 部署。
- 每个进程加载同一份显式配置；API 的 readiness 与受理会重新检查持久根、临时路径与读写权限策略。

### 3.2 一致性备份

```bash
uv run workbench db backup --config "$WORKBENCH_ROOT/config/local/agent.yaml" \
  --destination /absolute/backup/agents-20261118.sqlite3
```

- 使用 SQLite Online Backup API，从只读源读取，包含已提交的 WAL 内容，源库可保持在线。
- 每次备份/恢复/确认共享一个 **5 秒维护期限**；嵌套复制、校验、哈希与文件系统阶段都消耗同一预算，任何阶段都不重置。
- 发布前依次校验：完整性、外键、当前迁移 revision、检查点编解码/命名空间/父链/pending 索引、产物元数据与引用。
- 目标主文件或任何 `-wal` / `-shm` / `-journal` 同伴文件已存在（含符号链接）时**拒绝**，不删除冲突文件。
- 输出会给出 SHA256 与 checkpoint / pending / artifact / action 计数。
- 错误信息不打印行内容与密钥。

对象存储必须**独立备份**：数据库里的产物元数据不能证明对象存在、内容或完整性。

### 3.3 恢复到新路径

先停止将使用目标库的进程，保留源库与备份，**绝不**覆盖活动库或删除其 WAL/SHM。

```bash
uv run workbench db restore --config "$WORKBENCH_ROOT/config/local/agent.yaml" \
  --source /absolute/backup/agents-20261118.sqlite3 \
  --target /absolute/local/persistent/dod/restored.sqlite3
```

恢复会重复完整性、外键、Schema、检查点负载与元数据、命名空间哈希、完整父链（含环）、pending 写引用、产物元数据与证据引用校验。不兼容的编解码、缺失父节点、孤儿写或非法产物引用都会拒绝恢复。

- 恢复只适用于兼容的 Schema、存储协议与检查点编解码；**不**把旧 Workspace 库转换为 Agent 库。
- 原有 Run/Job/版本/回执/标记数据保持不变；`unknown` 状态的 Action 不会被当作已完成或自动重试。
- 目标只有先写入 `restored.sqlite3.restore-quarantine.json` 才会发布，其中声明 `external_writes_enabled=false`、`dispatcher_enabled=false`，并要求对账对象与动作。

### 3.4 隔离状态与重开

隔离标记存在时：

- readiness、`doctor` 和**每一个普通写 UnitOfWork** 都拒绝该库；写入方在取得 `BEGIN IMMEDIATE` 后会再次检查隔离标记。
- 只读 SQLite 检查仍然可用。
- 标记文件必须与数据库一起保留；**不得手工删除标记**来绕过门禁。

审阅完成后，仅当库中 **Artifact、Action、Outbox 均为 0 行**时才能确认重开：

```bash
uv run workbench db acknowledge-restore --config "$WORKBENCH_ROOT/config/local/agent.yaml" \
  --target /absolute/local/persistent/dod/restored.sqlite3 \
  --acknowledge-no-external-effects

export WORKBENCH_DATABASE_URL=sqlite+aiosqlite:////absolute/local/persistent/dod/restored.sqlite3
uv run workbench doctor      --config "$WORKBENCH_ROOT/config/local/agent.yaml"
uv run workbench scheduler   --config "$WORKBENCH_ROOT/config/local/agent.yaml" --once
uv run workbench worker      --config "$WORKBENCH_ROOT/config/local/agent.yaml" --once
```

该命令在持有排他写锁期间重新校验副本、写入 `restore-review.json`（确认时间与数据库文件哈希），然后移除隔离。它不改写任何业务/动作状态；发布失败则保留隔离。

若结束时报 `acknowledgment_committed=true, cleanup_completed=false`，表示门禁已提交打开但清理未完成：查看持久审阅记录与存储就绪，**不要**盲目重试确认，也不要自造回滚。

### 3.5 恢复后的对账

- 有 Artifact / Action / Outbox 的副本**不能**用上述 M1 命令重开；需要独立审阅的离线对账流程（当前未提供工具）。
- 对账前不得在新副本上重发任何外部动作；`unknown` 必须先向可信连接器查证。
- 缺失的固定图能力只能通过部署同一版本实现恢复；不得改写 `runs.versions`、就地修改已发布版本，或用当前图替换检查点。

---

## 4 Runner 与产物

### 4.1 产物上传

- 配置 API 与 Worker 共同可访问的 `storage.upload_staging_root` 绝对路径；生产环境限制为服务用户可访问。
- 流程：`POST /v1/runs/{run_id}/uploads` 建 `uploading` 记录 → `POST /v1/uploads/{artifact_id}/complete`（带 `Idempotency-Key`）。后者只落暂存目录并受理持久任务；Worker 校验长度与 SHA-256、写对象存储、回读校验，再校验租约/成员/范围后才标 `available`。
- 暂存根必须被同一数据库的 API 与 Worker 共享，文件系统要可靠支持跨进程 `flock`；根目录记录数据库路径摘要，禁止与其他库共用。
- 默认配额 256 MiB / 1024 个文件；回收只删除未被非终态上传 Job 引用的文件。暂时性存储故障保留在途文件；撤权或校验失败进入失败终态。

### 4.2 对象存储

- 仅 Worker 在启动时解析 `storage.s3.endpoint_ref` / `access_key_ref` / `secret_key_ref`，这些字段是**引用**；还需 `bucket` 与 `region`。
- 生产与 test 只允许 HTTPS；development 仅在显式 `allow_http` 且 endpoint 为数字回环地址时允许 HTTP。
- 后端需支持条件 PutObject 与 SHA-256 校验。本机 MinIO 已验证上传、回读与摘要；其他 S3 兼容服务未验收。
- 下载接口依赖注入的签名器；生产签名器装配尚未完成。

Skill 多文件草稿与产物上传分别装配：共享库发布及 Worker 加载仍使用 `storage.skill_object_root` 本地目录，API/Worker 必须共同访问同一内容；配置 `storage.s3` 不会自动迁移 Skill。

升级到 `0006_multifile_skill_drafts` 前先停止写入并备份数据库与对象目录，再使用既有 `workbench db migrate` 维护命令。该迁移允许已有 Agent/Profile/Run 数据的 v5 库，原草稿正文转换为 `files_json["SKILL.md"] = {encoding:"utf-8", content:原正文}`，保留修订号、时间戳、已发布引用和外键。服务不会启动时自动迁移；回退需恢复已核对的备份。更早、尚未完成 Graph ID 迁移的已填充库仍保留原有空库迁移限制。

共享库文件编辑器可保存脚本和 PNG/JPEG/GIF/WebP 图片；脚本保存不会执行，图片发布时写原始字节，草稿内为 Base64。每份草稿最多 64 个文件、解码后 8 MiB；文本附件最多 256 KiB，图片最多 2 MiB，主正文最多 32000 UTF-8 字节。`.claude` 自动执行配置仍不接收。Agent 专属导入接口保留现有脚本/二进制限制，本次扩展的是共享草稿链路。

### 4.3 仓库快照

- `storage.repository_sources` 必须显式绑定 `team_id`、`agent_id`、仓库名与本地绝对路径。
- 同步 API 只接受完整 Git commit，通过持久管理任务生成有界、拒绝符号链接的归档；当前不从远端抓取。
- Agent Profile 引用不可变快照后，新 Run 固定该引用。

### 4.4 Runner 绑定

`storage.runner_bindings` 默认为空。启用时必须：

- 默认 Docker 模式：`storage.runner_daemon_id` 是对应 daemon 的**真实 ID**（`docker info --format '{{.ID}}'`），不是机器别名；不同 daemon 各用自己的 ID。
- `storage.docker_host` 固定为本地 Unix socket（默认 `unix:///var/run/docker.sock`），不支持 TCP daemon，不使用可变 Docker context。
- 绑定明确指定团队、Agent、快照标识与哈希、镜像摘要、只读 snapshot volume、Python 脚本路径与 SHA-256。
- 每次创建/清理前后都重新校验 daemon 身份；不匹配则不创建资源，也不解除清理义务。
- 移除执行绑定后，保留 daemon 身份、socket 与维护能力，直到待清理记录处理完毕。

运行期还要求：活动 Worker claim、当前成员、五层工具权限、Run 预算、唯一调用记录；执行前从已固定归档核对脚本与文件清单，容器内再次校验 volume。容器关闭网络、去除 capabilities、只读根文件系统，并限制 CPU、内存、进程数与临时卷大小。

只读检查：

```bash
uv run workbench runner check --config "$WORKBENCH_ROOT/config/local/agent.yaml"
```

检查 daemon 身份、固定镜像是否存在、总 CPU/内存至少 2 核 2 GiB。它不拉镜像、不跑容器；`ready` 不代表有空闲资源、隔离有效或脚本链路可用。

**Podman 兼容模式**（仅在显式设置 `storage.runner_identity_mode: podman_volume` 时）：

- 管理员先在目标 daemon 上手工创建独立空卷 `dod-runner-identity-<32 位小写随机十六进制>`。
- `storage.runner_daemon_id` 设为 `podman:<相同 32 位值>`。
- Runner 只读取身份卷，不创建也不修复；卷缺失或指向未预配的 daemon 时失败关闭，清理记录保持 pending。
- 身份卷是部署信任根：不要在不同 daemon 克隆、重建或复制；迁移 daemon 前先处理旧的待清理记录。
- Runner 在 Podman 上先 kill 再删除容器，避免兼容接口的停止宽限超过清理超时。

### 4.5 清理义务与终态

- 创建资源前持久化调用与 daemon/容器/卷的清理义务，并记录派发与确认。
- Run 失租后由独立维护任务按 daemon 身份与维护租约重试，不使用过期 Run Guard 写结果；失败保留 pending、记录次数并退避。
- 创建请求已派发但未确认时，即使资源暂不存在也保持 pending，避免 daemon 晚到创建后无人清理。
- 终态收据保留每个输出流最多 8 KiB 的脱敏文本、截断标记与摘要；未确定结果标 `unknown` 且禁止重发。

---

## 5 预算策略

### 5.1 受理时固定

Run 受理事务同时写入预算策略，早于任何 Worker 领取或预留；被拒绝的预留、崩溃和租约接管都不能改写它。

- 默认 `summary` 预留 = 一次模型调用 + 冻结上下文中 summary 角色模型版本的 `max_context_tokens`。
- 当前 64,000 token 模型配 `qa` 80,000 token 预算时，专家与评估共享 16,000 token。
- 上下文上限与父 token 预算**分别**检查。

`POST /v1/runs` 可传 `summary_token_reserve`（正整数，不超过冻结 summary 模型的上下文上限）：

```json
{
  "agent_id": "00000000-0000-0000-0000-000000000001",
  "mode": "qa",
  "input": {"type": "text", "text": "Investigate the failure"},
  "budgets": {"tokens": 2000},
  "summary_token_reserve": 1000
}
```

只在摘要请求的输入+输出安全上界确实放得下时才调小。超出固定预留的摘要请求直接拒绝，不能借用其他工作的额度。总预算低于预留是允许的，但网络阶段必须保留该预留，否则走确定性的不完整 Outcome。

`BudgetService(summary_tokens=...)` 只做一致性检查：与服务已受理的策略不同即 `BUDGET_POLICY_CONFLICT`；改进程配置永远不会改写已有 Run 的额度。要换策略就新建 Run。

### 5.2 历史 Run 没有策略

升级后没有预算策略的 Run（`summary_token_reserve: null`）仍可查询、查看预算快照、产生确定性 partial；新预留以 `BUDGET_POLICY_UNINITIALIZED` 失败关闭，并给出 `/v1/runs` 的 `create_run` 链接。

处置：

- 保留 Run 与账本用于审计，先审阅是否存在外部动作。
- 需要继续时，用新的幂等键（以及新的 `client_message_id`）创建替代 Run，显式给出 reserve 或使用版本化默认值。
- 若旧 Run 占着 Thread 活动位，用正常取消接口释放；不要删除历史，也不要重发未确定的调用。
- 没有自动回填或绕过命令；确需沿用原 Run 时，只能在权威策略与用量证据支持下做离线对账。

---

## 6 可观测性

API、Worker、Scheduler 的日志默认以 JSON 输出到 stderr；Tracing 与 Langfuse 默认关闭。普通日志不含模型问题与回答正文。

### 6.1 启用

在部署使用的 `agent.yaml` 中加入（只写引用，不写真实凭据）：

```yaml
observability:
  logging:
    level: INFO
    format: json
  tracing:
    enabled: true
    sample_ratio: 1.0
  langfuse:
    enabled: true
    base_url_ref: env://LANGFUSE_BASE_URL
    public_key_ref: env://LANGFUSE_PUBLIC_KEY
    secret_key_ref: env://LANGFUSE_SECRET_KEY
    content_mode: metadata_only
```

在每个服务启动终端导出三个 `LANGFUSE_*` 环境变量后重启。端点要求 HTTPS，仅非生产允许 localhost HTTP。缺少引用或凭据会让启用该功能的进程启动失败。不要写进命令历史、前端或版本库。

### 6.2 使用与限制

- 用日志中的 `run_id`、`trace_id` 关联 API 与 Worker，再到 Langfuse 查看对应 Trace 与 Generation。
- 受理前未启用 Tracing 的历史 Run 没有原始受理关联。
- `metadata_only` 不采集提示词、Skill 正文或答案；`redacted_content` 需数据安全审批，默认各限 64 KiB，脱敏不能保证清除全部个人信息与秘密。
- SDK 有界后台批量导出，进程关闭最多等待 5 秒；强制终止可能丢数据。
- 断网、限流、队列满与真实 Langfuse 联调仍需在目标环境验收。

追踪 ID 的组织方式见 [02 架构 §14](02-architecture.md#14-可观测性)。

---

## 7 动作策略与人工重试

### 7.1 默认关闭

服务启动不会自动安装外部发送器，也不会从进程配置恢复数据库中的动作策略。策略**只**通过显式同步写入：

```bash
uv run workbench action-policy status --config /path/to/deployment.yaml
uv run workbench action-policy sync   --config /path/to/deployment.yaml --expected-revision 0
```

- `service.action_grants`、`service.tool_permissions`、`service.connector_identities` 作为一个整体同步到同一数据库。
- 授权项必须明确团队、Agent、冻结的工具版本引用、具名人类审批者与完整精确参数。
- 首次同步用期望版本 0；后续先读当前版本再传 `--expected-revision`。过期同步被拒绝，避免旧配置覆盖撤权。
- 同步本身不发送任何外部动作；策略为空或未同步时不允许新动作。
- **只改配置文件不等于撤权**，必须再执行一次 `sync`。

同步用短写事务发布单调递增版本，审批绑定该版本；策略变化后，旧审批不会因稍后恢复相同配置而复活。API、Worker、Dispatcher 与对账在各自事务中读取当前版本。已提交的发送标记是授权的线性化边界：之后的请求可能已发出，撤权不能撤回该次派发，只能按回执或 `unknown` 对账。

策略与同步权限归部署管理员；配置不得含密码、令牌或凭据，认证只用部署方密钥引用。数据库恢复必须先核对历史 Action 与 Outbox，不在恢复副本上盲目重发。

### 7.2 人工重试

仅当可信适配器证明请求**未发送**、且自动重试八次仍失败时，具名审批者可请求人工重试：

- 原 Action 与 Outbox ID 不变，`attempt` 保留累计次数；每次人工请求只允许一次额外尝试。
- 要求审批未过期、当前策略版本、原发起人与审批者权限、Scope 均仍有效。
- 已发送但结果未知的动作**禁止**人工重发，必须先由可信连接器查证并对账。

自动退避可能跨越 30 分钟审批有效期；过期后不再派发。对"可靠确认未发送"的失败，若范围、参数、工具版本、部署策略与双方权限均未变化，可用幂等键请求重新审批：系统新建一条 30 分钟审批记录，不延长也不覆盖旧记录，沿用原 Action/Outbox 与累计尝试次数，仍受八次自动尝试上限约束。

> 当前只提供本地合成连接器测试；真实 Jira / SeaTalk / DoD 发送器与目标白名单尚未装配，命令成功**不**代表外部投递已验证。

---

## 8 合成接入

`synthetic-chat-v1` 与 `synthetic-alert-v1` 是开发测试协议，**不是** SeaTalk 或任何真实告警平台协议；生产环境启动时拒绝这些绑定。

### 8.1 配置

在 `service.synthetic_ingress` 中配置连接器名称、`protocol`、`team_id`、`agent_id`、`secret_ref`：

- 聊天还需 `actors`（外部身份 → 当前团队主体映射）；告警需固定 `actor_subject`。
- `secret_ref` 只接受 `env://变量名`，真实值由环境注入。
- 每个映射主体还需在 `service.connector_identities` 有匹配 team/agent 的有效绑定，且是当前有效团队成员并有 operator 权限。

### 8.2 签名与限制

入口 `POST /v1/ingress/{connector_id}`。签名为 HMAC-SHA256，输入是五个按序长度前缀字段：`DOD-SYNTHETIC-MAC-V2`、协议名、URL 中的连接器名、十进制秒时间戳、未修改的原始请求体；每字段以四字节无符号大端长度开头。请求头 `x-dod-timestamp` 与十六进制 `x-dod-signature`。

- 时间偏差 ≤ 5 分钟；请求体 ≤ 64 KiB；`occurred_at` 超过 7 天的事件拒绝（补录入口尚未实现）。
- 聊天事件字段：`event_id`、`thread_id`、`actor`、`text`、`occurred_at`。
- 不带 `resume` 的事件总是创建新的排队 Run；恢复必须显式传 `resume`（`run_id`、`interrupt_id`、`expected_revision`、符合中断 Schema 的 `response`）。事件回执与恢复请求同事务提交；重复事件返回同一受理结果，内容变更冲突。
- 事件与线程映射保留 90 天。

### 8.3 未完成

真实 SeaTalk challenge、回调签名与事件 Schema、告警平台协议、跨来源正文与链接归一、显式历史补录、真实环境验收均未实现。不要把它记录为真实连接成功，也不要在回调中发消息。

---

## 9 托管模型密钥

管理端可对已保存的模型输入 API Key。服务端用 Fernet 加密，保存在 `$WORKBENCH_PERSISTENT_ROOT/model-secrets/`（文件 `0600`、目录 `0700`）；数据库与接口只保存 `secret://managed-model/...` 引用，保存操作记审计但不记密钥值。旧 `env://...` 凭据与部署白名单继续可用。

### 9.1 生成主密钥（一次）

```bash
mkdir -p "$HOME/.config/my-workbench"
chmod 700 "$HOME/.config/my-workbench"
cd "$WORKBENCH_ROOT/apps/agent"
uv run python -c 'import os; from cryptography.fernet import Fernet; p=os.path.expanduser("~/.config/my-workbench/model-master.key"); f=os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600); os.write(f, Fernet.generate_key()); os.close(f)'
```

命令不会覆盖已有主密钥。

### 9.2 启动时注入

API 与 Worker 启动前设置相同的 `WORKBENCH_PERSISTENT_ROOT` 与主密钥，然后重启：

```bash
export WORKBENCH_MODEL_SECRET_MASTER_KEY="$(<"$HOME/.config/my-workbench/model-master.key")"
```

### 9.3 使用与注意事项

1. 保存模型草稿 → 输入 API Key 并保存密钥 → 发布模型版本。
2. 在 Agent 管理中选择该版本并发布 Agent，新 Run 才使用；旧 Run 保持固定版本。
3. **不要删除旧凭据文件**。
4. 团队共享模型的密钥可供同团队且经部署白名单授权使用该地址的 Agent；Agent 专属模型的密钥仅限该 Agent。
5. `service.model_credential_bindings` 中必须有匹配的团队、Agent 与地址；管理端密钥不要求与该条目的环境变量引用相同。
6. `service.managed_model_endpoints` 控制可使用管理端凭据的 HTTPS 地址，默认仅 `https://api.deepseek.com`；增加其他地址前先确认可信。

主密钥文件与密文目录分别备份并限制访问：丢失或更换主密钥会使旧凭据无法解密。目录与文件须由运行 API/Worker 的同一系统用户持有；多机部署需安全共享密文目录与主密钥，不能各机不同。保存密钥失败不会自动退回环境变量凭据。Worker 启用该存储时，即使 `content_mode: redacted_content` 也不导出模型输入输出正文。

该本地文件方案不代替集中式密钥管理；生产应使用 HTTPS、OIDC 与受控密钥分发。

---

## 10 排障速查

| 现象 | 先查 | 常见原因 |
| --- | --- | --- |
| `/health/ready` 不就绪 | `workbench doctor` | 持久根非绝对路径、Schema revision 不匹配、SQLite 版本过低、库被隔离标记 |
| 启动即退出 | 启动日志 | 配置引用缺失（模型/Langfuse/对象存储）、主密钥未注入、端口占用 |
| `/app/` 返回 503 | `WORKBENCH_FRONTEND_DIST_ROOT` 与 `apps/web/dist` | Web 未构建或路径不是绝对路径 |
| Run 一直 `queued` | Scheduler/Worker 是否运行、`run_jobs` 状态 | Worker 未启动、团队额度已满、Thread 已有活动 Run |
| Run 卡在 `running` 且无进展 | 租约与恢复扫描日志 | Worker 被 SIGKILL，等待租约过期后接管 |
| 恢复后写入被拒 | 是否存在 `*.restore-quarantine.json` | 恢复了副本但未审阅；按 §3.4 处理 |
| 预留失败 `BUDGET_POLICY_UNINITIALIZED` | 该 Run 的预算快照 | 历史 Run 没有策略；按 §5.2 新建 Run |
| `BUDGET_POLICY_CONFLICT` | 进程配置与 Run 快照 | 进程显式覆盖了 `summary_tokens`；删除覆盖或新建 Run |
| 动作一直 `waiting_approval` | `action-policy status`、审批有效期 | 策略未同步、审批过期（默认 30 分钟） |
| 动作 `unknown` | 对端回执 | 发送后未确认；先查证，不要重发 |
| 产物不可下载 | 上传 Job 状态、对象存储回读 | 校验失败、租约过期、签名器未装配 |
| `runner check` 非零 | daemon 身份 / 镜像 | ID 与 daemon 不匹配、镜像不存在、资源低于 2 核 2 GiB |
| 技能发布 `OBJECT_STORAGE_UNAVAILABLE` | `WORKBENCH_SKILL_OBJECT_ROOT` | 未设置、非绝对路径、API 与 Worker 不可共同访问 |
| SSE 无事件 | `/health/ready`、浏览器网络面板和代理缓冲 | API 未就绪、代理缓冲或 Redis 不可用；页面在连接反复失败后以 3 秒轮询降级 |
| MCP `check` 通过但查询失败 | 上游数据源与凭据 | `check` 只验证配置，输出 `business_query: not_checked` |
| 日志缺模型正文 | `content_mode` | `metadata_only` 默认不采集；托管密钥模式始终不导出正文 |

Redis 连接失败不会阻断受理、Job 发现与恢复：数据库轮询是权威来源，SSE 最多 1 秒回退。不要把 Redis 恢复当成 Run 恢复的前提。

## 内置知识库升级与索引维护

升级已有受支持的 Agent SQLite 库前，停止 API、Worker、Scheduler 等写入进程并备份；确认运行时 SQLite 支持 FTS5 trigram。使用目标版本执行 `uv run workbench db migrate --config "$WORKBENCH_ROOT/config/local/agent.yaml"`，把库迁移到[数据字典](06-reference/02-data-dictionary.md)所列当前 head；`0003_run_trace_contexts` → `0004_knowledge_base` 只是最初的知识库迁移，不是当前升级终点。再运行 `uv run workbench doctor --config "$WORKBENCH_ROOT/config/local/agent.yaml"`，检查 revision、完整性和外键后启动进程。迁移失败时保留原库并检查原因；降级需恢复已审阅的兼容备份，不能手工 stamp revision。旧 Workspace 库仍不能沿 Agent 链升级。

索引检查只读，修复需明确指定条目和当前 revision：

```bash
uv run workbench kb check-index --config "$WORKBENCH_ROOT/config/local/agent.yaml" --team-id "$TEAM_ID"
uv run workbench kb rebuild-index --config "$WORKBENCH_ROOT/config/local/agent.yaml" --team-id "$TEAM_ID" --entry-id "$ENTRY_ID" --expected-revision "$REVISION"
```

核对检查输出后再决定是否重建；普通查询不会静默修复索引。内部查询默认禁用，启用前配置服务身份、令牌和具体范围，并配套 MCP Server 数据源及独立出站凭据，参见[配置参考](06-reference/03-configuration.md)。

## 专项上线与真实验收

### 知识库与多文件 Skill

知识库升级后核对发布版本、FTS 当前行、引用版本和缺口关联；备份恢复验收应覆盖多个版本、归档、回滚及缺口，确认版本 hash 和索引一致。索引缺失、孤立、旧版本或 hash 不符时先只读检查，再用带 team、entry 和 expected revision 的显式维护命令修复；不得在普通查询或 doctor 中自动修复。服务凭据要绑定具体团队、source 和服务/环境/区域范围；无匹配与上游不可用要分别呈现。只有真实 MCP 链路、授权和范围检查通过后，部署配置才能声明上游已强制隔离。

多文件 Skill 使用 `0006_multifile_skill_drafts` 迁移；升级前同时备份 SQLite 数据库及 `storage.skill_object_root` 本地对象目录，升级后检查旧草稿主文件、附件、历史版本与 Agent 冻结引用。API 和 Worker 必须看到同一对象目录；`storage.s3` 不会自动迁移 Skill 对象。脚本附件的保存和发布不会执行脚本。已有运行数据的 v5 → v6 升级应走显式迁移，不要求清库；旧版本回退须恢复相配的数据库与对象目录备份。

### 评测采集与夜间比较

真实 A4 前在隔离本机配置中确认 API/MCP、模型密钥引用、实际价格与币种、总预算、知识来源与团队授权。先用 1–3 条独立冒烟核对版本片段、证据页面、费用和范围，再采集至少 30 条 dev 业务题并组织逐题人工评审；至少 12 条 holdout 独立保管。合成数据只能用于 engineering/dev，不能改名为 business；缺评审时业务质量为 unavailable，不能显示通过。未知计费显示待确认，多币种不直接合并。

运行、恢复或评分 holdout 时，向 `workbench eval run` 或 `workbench eval score` 提供 `--purpose release --release-id <发布标识> --holdout-permit <许可文件> --holdout-ledger <账本文件>`。许可文件为受信操作者维护的 JSON 对象，包含匹配的 `release_id` 和 `"active": true`；恢复时重新读取。账本按发布标识和数据集指纹去重，不记录题目、答案或凭据；账本损坏或不可写时拒绝操作。操作方必须保护许可文件和账本的访问与完整性：本地文件许可不是独立审批、身份认证或不可篡改审计。工具对话评测仍仅允许 dev。

评分时必须提供实验目录中原有的 `manifest.json` 或历史 `experiment.json`，数据集指纹和 split 须与之匹配；缺失实验记录的历史结果不能直接评分，应先核对原始记录，不能自行推测并伪造。新实验不能复用已有数据的目录，恢复已有实验使用 `--resume`。

启用 `experiment_budget_micros` 时，`pricing` 必须包含币种、价格版本、所有可能使用的模型中最高的输入和输出单价，以及 `tool_call_micros` 和 `scope: all_run_models_and_tools`；工具单次上界只有确认所有授权工具免费时才能填 0。预留按 Run 总 token 上限及最大工具调用数计算，不把未知费用当作零。价格是受信操作者提供的保守上界，实际供应商账单仍需核对；实际费用超出预留会停止后续新 Run。旧实验若已有发送记录但没有可核对的预留，不得修改冻结配置强行恢复，应先对账或新建实验。

夜间采集默认关闭；当前工作流仅执行预检与已有实验的比较，尚未采集运行或下载实验产物。目标方案是仅由采集机读取经批准的资料并持有模型/知识凭据，CI 仅接收脱敏白名单摘要；当前令牌检查尚未接入实际获取流程。首次手动运行时检查制品中没有问题、答案、证据、完整 URL 查询、评审正文、密钥或内部路径；核对来源 workflow、仓库、分支、成功状态、新鲜度与 checksum 后，才启用定时。连续三个调度日要产生不同 candidate ID 和实际运行数，并验证一次缺候选路径；未满足前不写“nightly 已验收”。停止方式为关闭采集调度与 CI 开关；预算耗尽时保留实验 journal，核对后以同一 experiment ID 恢复，不能自动创建无限新实验。更换基线须记录数据/模型/工具指纹和评审依据。

### 审核中心、网页搜索与部署边界

审核中心验收需在真实浏览器中逐一走通输入、Run 审批、Action 审批、重审和对账；检查 viewer/operator/maintainer、撤权、等待过期和两名操作员竞争。摘要自动刷新不代表待办可以由页面自行判定；提交前由后端按当前权限和状态再次校验。`unknown` 投递只做只读 lookup，无法确认时继续保留 unknown，绝不据此自动重发。

`web_search` 上线前由部署方提供兼容 MCP、凭据、服务端强制域名策略和只允许公开资料的 Agent。用 `knowledge_search` 与 `web_search` 双工具验证公开问题、权限收窄、空/失败/截断/超时、恶意网页文本和来源 URL 展示。网页摘要不是全文或已证实引用；不得给含内部知识或内部问题的 Agent 授权 `web.read`。离线合同和 UI 测试不构成真实上游签收。

目标部署还需记录 R2 在实际库上的查询计划和耗时，检查 10,000 来源行容量；核对 Docker/Compose、TLS/代理、当前授权撤销、备份恢复、真实 Jira/SeaTalk/DoD 回执和生产 Runner 隔离。之前离线测试在限制沙箱内的 localhost/子环境错误曾于沙箱外复跑通过，但 CI 仍应在能监听 localhost 的 runner 上取得自己的结果。工作树 `.venv` 与主工作区 Python/SQLite、桌面 Node 与系统 Node 的差异只属于测试环境说明，不可作为产品能力或真实验收结论。

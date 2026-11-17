---
title: "DoD M1：连接与运行环境验收记录"
date: "2026-11-12"
status: task-1-foundation
---

## 验证层次

配置、发现、真实调用分别记录。`dod doctor --config PATH` 只验证本地配置及 SQLite 存储，不访问外部服务、不执行工具或迁移，不产生模型付费调用。退出码 0 表示存储检查通过，1 表示配置或存储未就绪；不是所有外部能力的健康证明。

| 依赖 | 配置 | 发现 / 连通性 | 真实业务调用 |
| --- | --- | --- | --- |
| Python / SQLite | Python 3.12；SQLite 下限 3.51.3 | 本机 Python 3.12.3 链接 SQLite 3.53.1 | 真实文件诊断通过；迁移由 T02 交付 |
| Schema / Codec | `dod-storage-v1` / `dod-checkpoint-v1` | 测试库单行元数据检查通过；未迁移库 unready | 尚无业务表 / Checkpoint 写入验收 |
| DeepSeek | `deepseek-dod-default@1` 种子；`https://api.deepseek.com`；`deepseek-flash`；`env://DEEPSEEK_API_KEY` | 未探测 | 未调用；没有付费探针 |
| MCP | 部署环境尚未指定具体连接 | 未发现 | 未调用 |
| 对象存储 | `env://WORKBENCH_S3_ENDPOINT`、`dod-artifacts`、`secret://dod/object-storage` | 未探测 | 未读写 |
| Redis | `env://WORKBENCH_REDIS_URL`；不作为 admission 前置条件 | 未探测 | 未调用 |
| OIDC | `env://WORKBENCH_OIDC_ISSUER`；audience `workbench-agents` | 未探测；local 明确采用 development 身份 | 未验证令牌；production 拒绝 development 身份配置 |
| Runner | `python312-tools-v1` 镜像 Profile | 未注册；需管理员映射实际发布 digest | 未执行脚本 |
| 运行镜像 | 官方 Python OCI digest 固定；SQLite 3.53.1 源码 SHA256 固定 | 已向官方 Registry 解析 digest；下载计算源码 checksum，并检查 configure 参数 | 无 Docker CLI，未构建、未运行；镜像链接库验收待有 Docker 的环境完成 |

## 使用方式

在项目根目录准备依赖和持久目录，通过显式环境变量指定资源与数据库。目录声明不能代替部署方对本地持久磁盘的保证，不要放在 NFS、内存文件系统或临时卷。

```bash
uv sync --frozen
mkdir -p var
export WORKBENCH_ASSET_ROOT="$PWD"
export WORKBENCH_PERSISTENT_ROOT="$PWD/var"
export WORKBENCH_DATABASE_URL="sqlite+aiosqlite:///$PWD/var/dod.sqlite3"
uv run workbench doctor --config config/local.yaml
```

T01 不创建数据库，故新目录诊断退出 1 是预期结果。T02 维护迁移创建 Schema 和元数据后再验证。普通进程不执行 DDL、不改变持久化 `journal_mode`。诊断使用 `mode=ro` 连接，设置并检查连接级 `foreign_keys=ON`、`busy_timeout=5000`、`synchronous=FULL`、`wal_autocheckpoint=1000`，只读取持久化的 `journal_mode=WAL`。SQL 检查共享 5 秒总期限；锁等待按剩余时间缩短超时。

`persistent_root` 必须显式声明，数据库真实路径必须位于其中；检查本地文件系统、读写权限，production 拒绝临时目录。容器应挂载 `/var/lib/dod` 本地持久卷。Dockerfile 默认 local 配置是 development，生产须提供自己的环境与身份配置。

## 安装与版本接口

资源位于 wheel 外，`WORKBENCH_ASSET_ROOT` 必须指向现有绝对目录，没有 cwd 回退。安装回归在临时目录构建 wheel、创建干净环境并切换到源码外执行导入与 `dod --help`。目前只有 `settings` 与 `cli`，后续引入真实 `api`、`runtime`、`persistence`、`skill_management` 包时同一契约自动覆盖，不预建空业务包。

维护迁移负责创建 `dod_storage_metadata`，提供恰好一行 `schema_version TEXT` 与 `checkpoint_codec_version TEXT`。T01 检查这两个版本与 SQLite 能力；T02 必须扩展为实际业务 Schema 完整性检查。无行、多行、缺表或不匹配均 unready。

诊断仅返回脱敏路径占位符、能力、版本和固定错误类型。数据库 URL 使用 `SecretStr` 并排除序列化；其他密钥保持 `secret_ref`，不在文档或诊断输出实际凭据。OIDC 这里只检查部署配置结构，身份验证器及真实令牌验收由后续 API 任务完成。

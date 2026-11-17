---
title: "产物与 Runner：配置和当前限制"
---

## 产物上传

配置 API 与 Worker 可共同访问的 `storage.upload_staging_root` 绝对路径。生产系统应将该目录限制为服务用户访问，备份与容量策略由部署方管理。通过 `POST /v1/runs/{run_id}/uploads` 创建 `uploading` 记录，再以原始请求体调用 `POST /v1/uploads/{artifact_id}/complete` 并提供 `Idempotency-Key`。后者只将数据写入暂存目录并受理持久任务；Worker 验证长度和 SHA-256、写入对象存储、回读校验，再校验任务租约、当前成员和原查询范围后才将产物标为 `available`。失败或租约过期不会使该记录可下载。

仅 Worker 在启动时解析 `storage.s3.endpoint_ref`、`access_key_ref` 和 `secret_key_ref`；这些字段是引用，不是密钥。还需指定 `bucket` 和 `region`。生产和 test 环境只允许 HTTPS。development 环境只有显式设置 `allow_http` 且 endpoint 为数字回环地址时才允许 HTTP；本地 HTTP 请求不使用代理。存储后端需要支持条件 PutObject 和 SHA-256 校验。本机 MinIO 已完成 Worker 上传、回读和摘要验收；其他 S3 服务兼容性尚未验收。

本机开发可使用 Git 忽略的 `var/minio-local.yaml`：它只保存 `env://WORKBENCH_S3_ENDPOINT`、`env://WORKBENCH_S3_ACCESS_KEY`、`env://WORKBENCH_S3_SECRET_KEY` 引用，指向本机 `dod-artifacts` bucket，实际凭据仍保留在权限受限的 `var/minio.env`。以下验收仅访问 `http://127.0.0.1:19000` 和该 bucket，测试创建独立对象并验证删除；它使用隔离测试数据库与暂存目录，不验证生产部署。先确保本地 MinIO 正在运行：

```bash
(
  set -a
  . var/minio.env
  set +a
  unset MINIO_ROOT_USER MINIO_ROOT_PASSWORD
  uv run pytest tests/contract/live/test_minio_upload.py --run-live -q
)
```

暂存根必须由同一个 SQLite 数据库的 API 和 Worker 共享，且文件系统必须可靠支持跨进程 `flock`；根目录记录数据库路径的摘要，不允许其他数据库共用。默认配额为 256 MiB、1024 个文件。提交与回收共用文件锁，回收仅删除未被非终态上传 Job 引用的文件；Worker 周期回收及新上传时回收可清理崩溃孤儿和已完成文件。暂时性存储故障保留在途文件供恢复，撤权或校验失败进入失败终态。幂等重放以产物 ID 和幂等键为身份，并重新检查当前 Agent 权限；明确的纯重放需设置 `Upload-Replay: true` 且请求体为空。普通重放会流式校验正文长度与已固定 SHA-256，正文或产物不一致返回冲突；纯重放不依赖原 Scope。可用产物下载接口依赖注入签名器；API 的生产签名器装配目前尚未完成。

## 仓库和 Runner

Worker 的 `storage.repository_sources` 必须显式绑定 `team_id`、`agent_id`、仓库名与本地绝对路径。同步 API 只接受完整 Git commit，并通过持久管理任务生成有界、拒绝链接的归档；当前不从远端仓库抓取。Agent Profile 引用不可变快照后，新 Run 固定该引用。

`storage.runner_bindings` 默认为空；启用时必须配置 daemon 身份。默认 Docker 模式下，`storage.runner_daemon_id` 必须是对应 Docker Engine 的真实 ID（可通过该 daemon 的 `docker info --format '{{.ID}}'` 获取），而不是自定义机器别名；同一 daemon 使用相同 ID，不同 daemon 使用各自实际 ID。Podman 兼容模式使用下述显式预置的独立身份卷。`storage.docker_host` 固定为本地 Unix socket（默认为 `unix:///var/run/docker.sock`），当前不支持 TCP daemon；不使用可变 Docker context。每次创建、清理及确认清理后重新校验所配置模式的 daemon 身份，查询失败或不匹配时不创建资源、不关闭清理义务。移除执行绑定后，保留 daemon 身份、原 socket 和 Worker 维护能力直至待清理记录处理完毕。绑定明确指定团队、Agent、快照标识与哈希、镜像摘要、只读 snapshot volume、Python 脚本路径与 SHA-256。运行时还要求活动 Worker claim、当前成员、五层工具权限、Run 预算和唯一调用记录；执行前从已固定归档检查脚本和全部文件清单，容器内再次读取并校验 volume 内容，使用已校验脚本字节执行。Docker 可执行文件可用 `storage.docker_binary` 配置为绝对路径，默认使用固定受控 PATH。容器参数关闭网络、去除 Linux capabilities、只读根文件系统，并限制 CPU、内存、进程数及临时任务卷大小。部署方必须预先正确建立并保护 snapshot volume，确保只有可信组件能更改它。

配置 Runner 后，可运行 `dod runner check --config <配置路径>` 只读检查 daemon 身份、已配置固定镜像是否存在，以及服务报告的总 CPU 和内存是否至少为 2 核及 2 GiB。该命令不拉取镜像、不执行容器；`ready` 不保证当前可用空闲资源、隔离效果、脚本或产物链路，空绑定和检查失败均返回非零状态。

Runner 终态收据保留每个输出流最多 8 KiB 的脱敏文本、截断标记及摘要，可由接管的 Worker 读取；未确定发送结果仍标记 unknown 并禁止重发。脱敏识别常见密钥格式、凭据字段和私钥，但无法保证识别任意没有标识的敏感内容。创建资源前会持久化调用与 daemon、容器和卷的清理义务，并记录创建请求的派发与确认。Run 失租后，独立维护任务按 daemon 身份及维护租约重试，不使用过期 Run Guard 写结果；失败保留 pending、记录尝试次数并退避。若创建请求已派发但未确认，即使资源暂时不存在也保持 pending，避免 daemon 晚到创建后无人清理；需要继续维护或由运维调查。清理命令失败或超时会保留清理义务，不依赖原 Worker 的异常传播。

本机已通过 Podman Docker 兼容接口验证真实容器的网络隔离、CPU/内存和任务卷限制，以及固定 Git 快照执行、篡改拒绝、父子进程超时与取消清理。该测试使用临时身份卷；实际部署仍需按下文显式创建和保护独立身份卷。独立 Runner 服务鉴权、心跳、镜像与容量就绪检查、产物回收和跨 Worker 卷装配尚未完成；完整容器验收仍待进行。

### 本机 Podman 的独立身份绑定

Runner 默认使用 Docker Engine ID；只有显式设置 `storage.runner_identity_mode: podman_volume` 时才启用 Podman 兼容模式。部署管理员必须先在目标 Podman daemon 上手工创建一个独立的空卷，名称格式为 `dod-runner-identity-<32位小写随机十六进制>`，并将 `storage.runner_daemon_id` 设置为 `podman:<相同32位值>`。Runner 只读取身份卷，不会创建或修复它。身份卷缺失、检查失败、或配置指向另一未预配该卷的 daemon 时，执行和资源清理均失败关闭；清理记录保持 pending。

固定 `storage.docker_host` 到受信的本地 Unix socket，并配置受信 Docker 兼容 CLI 的绝对路径。每次创建、清理前后均在同一 socket 上检查身份卷。身份卷属于部署的信任根：不要在不同 daemon 上克隆、重建或复制同一身份卷，不要允许不受信任者访问容器 socket 或更改身份卷；本机制不抵御可控制容器服务的特权进程。迁移或重建 daemon 之前，须先处理旧 daemon 的所有待清理记录，不能仅复制配置和身份卷。此模式不改变 Docker 默认身份校验。

已在本地 Podman 的 Docker 兼容 socket 上以实际 SQLite Worker、固定 Git 快照和容器验证执行；本机 MinIO、容器测试与生产部署仍需分别验收。Runner 在 Podman 上先发送 kill 再删除容器，避免兼容接口的停止宽限超过清理超时；失败时仍保留持久清理义务。

---
title: "Skill 种子导入与版本加载"
---

## 当前支持范围

工作台的 Skills 页面提供团队级技能库：管理员可新建、编辑并发布纯文本技能；在 Agent 管理中选择已发布技能的具体版本，保存并发布 Agent 后，新任务才会使用该版本。`dod.v18` 会将绑定的 `SKILL.md` 指令放入模型上下文，Skill 不会自动获得工具权限或执行脚本。既有 Agent 私有技能保留原归属。

首次升级前，停止 API、Worker 和 Scheduler，并备份现有 SQLite 数据库及 Skill 对象目录。使用原有数据库路径执行以下命令，迁移会在现有 Agent 数据库上追加共享技能草稿表，不会重建数据库：

```bash
export WORKBENCH_SKILL_OBJECT_ROOT="$WORKBENCH_PERSISTENT_ROOT/skill-objects"
mkdir -p "$WORKBENCH_SKILL_OBJECT_ROOT"
cd "$WORKBENCH_ROOT/apps/agent"
uv run workbench db migrate --config "$WORKBENCH_ROOT/config/local/agent.yaml"
uv run workbench doctor --config "$WORKBENCH_ROOT/config/local/agent.yaml"
```

API 和 Worker 启动终端均须导出相同的 `WORKBENCH_SKILL_OBJECT_ROOT`，然后重启服务；目录必须是两者可访问的持久存储。后续仅发布 Skill 或更改 Agent 绑定无需重启。若未配置该目录，技能发布会返回 `OBJECT_STORAGE_UNAVAILABLE`。发布的版本内容与数据库需要一起备份。

部署方可在资产根目录的 `skills/<name>/SKILL.md` 放置纯文本 Skill。设置 `WORKBENCH_ASSET_ROOT`（或配置 `asset_root`）及 `storage.skill_object_root` 为 API 与 Worker 可共同访问的绝对目录；后者必须是受控持久存储，缺省时不会启用导入 Worker，发布返回存储不可用。当前对象适配器是本地内容寻址目录，不实现 S3/MinIO 或多机复制。

管理员调用 `POST /v1/agents/{agent_id}/skills/{name}/import`，提供 `Idempotency-Key`。该请求只受理持久管理任务；Worker 重新检查管理权限，读取受信种子目录，验证后将快照写入对象目录。通过管理任务 API 检查结果后，以 `If-Match` revision、`Idempotency-Key` 和 `{"job_id":"..."}` 调用同一路径下的 `/publish`。首次 revision 为 0，后续使用当前 Skill revision。Agent Profile 需要显式绑定返回的版本引用并发布，已有 Run 保持旧的引用。

还可通过 `/import/artifact` 提供同一 Agent 内已授权的可用 ZIP Artifact，或通过 `/import/git` 提供部署侧 `storage.skill_git_repositories` 白名单中的本地仓库名和完整 commit。Artifact 当前必须由同一个本地内容寻址对象目录提供；工作进程再次验证权限、大小及摘要。Git 只读取已提交内容，不读取未提交工作树，也不访问远程仓库。现有 Agent Profile 发布接口可将绑定回滚至旧版本。S3 对象、远程 Git 获取与垃圾回收尚未实现。保留对象目录和数据库备份，当前没有删除已发布版本的入口。文件、对象或摘要缺失时加载失败，不使用最新 Skill 代替。每个资源单独计算 SHA-256，和整个包的摘要不同。

目前仅接受 `name`、`description` 元数据和 UTF-8 非可执行文本资源；二进制、含 NUL 的资源在导入时拒绝；`.claude` 配置、Hook、脚本、可执行文件以及额外元数据都拒绝。尚未适配 Claude 工具名、变量和权限语义；不能当作已支持执行。导入不处理软链接或特殊文件，不允许路径越界、重复文件和超出大小限制的包。生产部署应限制资产目录和对象目录写权限，避免不可信用户更改。

真实知识检索仍处于安全关闭状态：上游 `task_context` 目前没有可证明的授权过滤语义。未连接真实知识源，不能将本地 Skill 测试当成知识引用验收。

加载器仅接受当前有效 Worker Execution 对应的冻结 Run 上下文；读取前后重新校验租约和当前成员。已发布对象以同目录临时文件和原子无覆盖链接写入，读取方不会看到未完成的内容；意外退出遗留的 `.pending-*` 文件不属于已发布对象。

此前已发布的 Skill 包即使含有二进制资源仍可通过完整性校验；历史二进制资源加载时返回 bytes，文本资源返回字符串。新导入继续拒绝二进制，直到完整的资产处理和安全契约就绪。

新导入版本以不可变内容寻址目录和独立资源保存。加载时先核对目录摘要与 `SKILL.md`，然后只读取所需资源并核对单资源摘要；此前已发布的 ZIP 格式仍可加载，历史 ZIP 需要整包校验。当前对 Claude 的 `allowed-tools`、`hooks`、`variables`、`permissions`、`tools` 和 `image` 等声明明确拒绝，不推断等价执行语义。

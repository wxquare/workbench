# 多文件 Skill 草稿实施与验证记录

日期：2026-11-23。开发分支：`codex/multifile-skill-drafts`。

## 已交付行为

- 一条 skill_drafts 记录以 files_json 保存完整资源包，资源编码为 `{encoding, content}`；主文件保存正文，发布时生成 YAML 元信息。
- 共享编辑器支持文本与脚本文件的新增、切换、修改、重命名、删除，以及 PNG/JPEG/GIF/WebP 图片上传、预览和替换。
- 历史版本可逐文件只读查看；切换 Skill 或历史版本不会应用过期异步响应。
- 发布保存所有原始资源字节和 manifest，版本固定、历史 Run 和 Agent 绑定协议保持一致；脚本保存和加载不会触发执行。
- 旧 content 请求和响应保留兼容；旧客户端保存主正文不会删除附件。文件集合完整保存支持删除和重命名。
- 新迁移 `0006_multifile_skill_drafts` 将旧 content 无损包装并更名为 files_json；支持有 Profile/Run 数据的 v5 数据库升级，不修改历史发布对象。
- 发布继续使用现有本地对象目录，不接入 S3。

## 验证结果

### 后端相关回归

```text
apps/agent/.venv/bin/python -m pytest \
  apps/agent/tests/integration/test_multifile_skills.py \
  apps/agent/tests/integration/test_multifile_skill_migration.py \
  apps/agent/tests/integration/test_shared_skills.py \
  apps/agent/tests/integration/test_skill_versions.py \
  apps/agent/tests/integration/test_skill_bundle_validation.py \
  apps/agent/tests/integration/test_skill_review_fixes.py \
  apps/agent/tests/integration/test_skill_import.py \
  apps/agent/tests/integration/test_agent_domain.py \
  apps/agent/tests/contract/storage/test_agent_boundary.py \
  apps/agent/tests/integration/test_graph_id_migration.py \
  apps/agent/tests/integration/test_kb_migration.py -q
```

结果：**106 passed in 21.44s**。覆盖图片和脚本的保存/发布/冻结加载、文件增删改名、空附件、旧请求保留附件、幂等、并发冲突、超限和非法路径/图片/Base64、对象缺失、迁移与存储完整性。

### 后端全量检查与环境复跑

命令：`apps/agent/.venv/bin/python -m pytest apps/agent/tests -q`。

首次在限制沙箱中执行：**1485 passed, 18 skipped, 4 failed, 8 errors**，耗时 716.41s。4 个失败为 wheel 临时环境创建、实际 CLI HTTP 监听、Redis 故障恢复、SSE/Redis 回退；8 个初始化错误均来自真实 MCP 测试的本地 socket.bind 被拒绝。

经授权在沙箱外复跑以下文件/用例：

```text
apps/agent/.venv/bin/python -m pytest \
  apps/agent/tests/contract/test_package_layout.py::test_installed_wheel_works_outside_repository \
  apps/agent/tests/fault/test_cli_startup.py \
  apps/agent/tests/fault/test_redis_outage.py \
  apps/agent/tests/integration/test_kb_mcp_chain.py -q
```

结果：**12 passed in 184.26s**（包含 1 项沙箱内已通过的 Worker 关闭测试）。

```text
apps/agent/.venv/bin/python -m pytest \
  apps/agent/tests/integration/test_sse_replay.py::test_configured_redis_connection_failure_falls_back_within_one_second -q
```

结果：**1 passed in 1.75s**。全量检查中的 12 个环境失败/错误均已复跑通过。全量运行之后新增的边界测试以及审查修复由上面的最新 106 项回归覆盖。

18 项跳过按项目现有策略执行，包含需显式启用的外部 live 集成。

### 前端

在 `apps/web` 执行：

- `npm test`：**41 files / 159 tests passed**。
- `npm run typecheck`：exit 0。
- `npm run build`：exit 0，1711 modules transformed。

测试使用真实 Vue 组件与自定义渲染器，覆盖脚本编辑、图片上传替换、主文件保护、同名重命名不丢文件、历史只读、过期上传/历史请求隔离。随后按用户要求完成真实内置浏览器验收，结果见下节。

### 独立审查

审查发现并通过失败测试复现后修复了两项问题：

1. 技能列表原先会读取和解码所有草稿图片；现仅读管理信息与版本引用。
2. 非法 Unicode 文件名可能导致编码异常；现作为 422 校验错误处理。

专项复查三项测试通过。全功能审查涵盖前后端、迁移、兼容和运行时边界，无剩余重要可执行发现。

### 文档与静态检查

- `git diff --check`：通过。
- 变更 Python 模块编译检查：通过。
- 已更新架构、API、数据字典和运维说明。
- `python3 scripts/check_docs.py`：剩余 **7 个既有 verified_commit 引用错误、4 个已有行数警告**。基线为 11 个引用错误；本次修改的 4 份参考文档已更新到当前有效祖先提交。未改动用户已有的 README、产品分析等其它文档工作。

## 升级与运行边界

升级前停止写入并同时备份数据库和 Skill 对象目录。按项目既有环境配置运行 `workbench db migrate --config ...`，再重启 API/Worker；普通服务不会自动升级旧库。回退需恢复经过核对的备份。

首轮自动化开发验证使用临时数据库。随后用户明确要求启动本地 8000 并通过内置浏览器创建测试 Skill，已按下节记录备份并升级本地数据库。本记录对应开发验收结束时的状态：源码保留在开发分支以供查看差异，尚未合并或推送。

## 本地内置浏览器验收

用户授权：启动本地 8000，使用 computer use 和内置浏览器创建多文件 Skill。

- 启动的是当前分支 API，配置为 `config/local/agent.yaml`，页面为 `http://127.0.0.1:8000/app/skills`。原占用端口的是另一工作树的 Workbench API，核对进程后以 SIGTERM 停止并替换；未启动 Worker/Scheduler。
- 本地数据库 `$WORKBENCH_PERSISTENT_ROOT/agents.sqlite3` 从 v5 升至 v6；之前做 SQLite 一致性备份并验证 quick_check=ok。备份路径：`$WORKBENCH_PERSISTENT_ROOT/backups/before-multifile-20261123-110757.sqlite3`。
- 既有两份 Skill 保留；通过页面创建 `browser-multifile-check-20261123`，共 5 个文件：主指令、参考 Markdown、Python 脚本、报告模板、PNG 图片。
- 通过页面保存后刷新，重新打开并核对主指令及脚本源码仍存在；图片成功预览。
- 通过页面发布 v1，历史显示 5 个资源，主文件包含自动生成的 YAML 元信息。
- 修改参考文档为 v2，模板由 `templates/report.md` 重命名为 `templates/report-v2.md`，更新主指令引用；新增并删除一份临时文件，最终仍为 5 个文件。
- 保存并发布 v2 后，Skill 显示版本 2、修订 3。查看 v1 仍有原模板路径和旧参考正文；查看 v2 有新模板路径和新增 PAY-3001 条目，历史为只读。
- 发布版本中的脚本源码可查看；历史 PNG 图片成功加载，DOM 图片尺寸为 1920×1716。
- 浏览器创建、编辑、上传、保存、发布和历史核对全部通过 computer-use UI 完成，未用直接 API 写入代替页面操作。
- 内置浏览器标签页已保留，测试 Skill 和两个发布版本保留供用户查看。

截图保留在本地浏览器验收会话附件中，文件名为 `browser-skill-v1-history.jpg` 与 `browser-skill-v2-image.jpg`，不随仓库提交。

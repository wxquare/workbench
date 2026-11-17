---
title: 可观测性配置与升级
---

# 可观测性配置与升级

API、Worker、Scheduler 的 `dod` 日志默认以 JSON 输出到 stderr。Tracing 和 Langfuse 默认关闭；启用时所有使用相同配置的进程都必须配置凭据。普通日志不包含模型问题和回答。

## 升级

停止 API、Worker、Scheduler，按 [SQLite 恢复手册](sqlite-recovery.md) 对数据库执行一致性备份，并同时备份 Skill 对象目录。确认配置指向现有 Agent 数据库，然后执行：

```bash
cd "$WORKBENCH_ROOT/apps/agent"
uv sync --frozen --group dev
uv run workbench db migrate --config "$WORKBENCH_ROOT/config/local/agent.yaml"
uv run workbench doctor --config "$WORKBENCH_ROOT/config/local/agent.yaml"
```

迁移 `0003_run_trace_contexts` 在兼容的 Agent 数据库中新增关联表，不修改历史任务内容。旧 Workspace 数据库不能使用此迁移链。确认 doctor 就绪后，在原来的独立终端分别启动 API、Worker 和 Scheduler；Worker 仍需原有模型密钥。完整启动命令见 [本地启动手册](source-layout.md)。

## 启用 Langfuse

在经批准的 Langfuse 项目中获取端点和项目密钥。将以下配置加入部署使用的 `agent.yaml`，不要写入真实凭据：

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

在每个服务启动终端分别导出 `LANGFUSE_BASE_URL`、`LANGFUSE_PUBLIC_KEY` 和 `LANGFUSE_SECRET_KEY`，然后重启服务。端点要求 HTTPS；仅非生产环境允许 localhost HTTP。缺少配置引用或凭据会导致启用该功能的进程启动失败。不要将密钥放进命令历史、前端或版本库。

新建任务后，可用日志的 `run_id`、`trace_id` 关联 API 和 Worker；在 Langfuse 中检查对应 Trace 和 Generation。受理前未启用 Tracing 的历史任务没有原始受理关联。`metadata_only` 不采集提示词、Skill 正文或答案。

经数据安全审批后可设置 `content_mode: redacted_content`，采集脱敏后的文本输入和回答，默认各限 64 KiB。脱敏只能降低风险，无法保证清除所有个人信息和秘密；确认项目访问权限、地区和保留期后再开启。SDK 不会采集供应商 HTTP 请求头，普通日志也不会输出模型正文。

SDK 使用有界后台批量导出；进程关闭时最多等待 5 秒，超时或强制终止可能丢失未导出的数据。模拟导出失败及退出等待超时已有测试，断网、限流和队列满仍需在目标环境验收。真实 Langfuse 服务的端到端验证需使用获准的项目与非敏感测试任务，本手册不代表已在目标环境完成联调。

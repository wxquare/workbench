---
title: 纯 LLM 问答：Agent 配置与运行
---

本文使用 Agent 领域重构后的契约，实现与验证边界见 [主规格 21.4](../../docs/specs/2026-11-12-workbench-python-spec.md#214-移除-workspace-的-agent-领域重构)。先按 [本地启动手册](../../docs/runbooks/source-layout.md) 显式选择新的 SQLite 路径并初始化。旧 Workspace 数据库必须保留；不得通过删除旧文件或启动时自动重建来启用新契约。

`dod.v17` 直接将问题发送给 Agent 的已发布模型，不查询知识源。结果会注明由模型生成且未经过外部知识源验证。历史任务仍使用提交时固定的工作流。

## 多轮会话和管理页面

“我的工作台”支持多个独立可配置 Agent，DoD Agent 只是其中一个场景。每个 Agent 对应 `agents.agent_id`，管理页面为 `/app/agents`，后端 API 为 `/v1/agents`；任务、会话、资源和授权直接归属 Agent，不再设置 Workspace 产品或领域层。历史任务留在旧数据库，新库通过显式重建配置开始使用。

Agent 草稿可设置描述、系统提示词、回复语言、回复格式、多轮历史数量以及运行预算。系统提示词和回复偏好在 `dod.v17`、`dod.v18` 直接问答中生效；语言和格式是对模型的指令，不保证严格遵循或输出有效 JSON。`dod.v18` 可使用 0 至 8 轮历史，实际数量仍受模型上下文和预算限制。配置保存为草稿后须发布，只有之后提交的任务使用新版本。`dod.v17`、`dod.v18` 不调用工具、Skills 或知识源。

新建 Agent 使用团队级已发布模型初始化，其他配置取新草稿的领域默认值，不继承现有默认 Agent 的系统提示词、回复偏好、历史窗口、预算或资源绑定。管理员需独立设置工作流和提示词，再保存并发布；缺少团队级模型时返回 `MODEL_PROFILE_UNAVAILABLE`，须先配置模型。默认草稿的 `dod.v3` 只记录输入；直接问答须显式选择 `dod.v17` 或 `dod.v18`，不能用已有默认 Agent 的配置推定新 Agent 已准备就绪。

创建不依赖现有活动 Agent，使用团队级幂等记录；`auth.agent_scope` 受限的身份不能创建范围外的新 Agent。模型、工具及凭据授权仍须为新 Agent 单独检查。

构建前端并重启 API、Worker 和 Scheduler 后，使用有管理权限的账号打开 `/app/`。在 Agent 管理页面选择 Agent，检查现有草稿，选择 `dod.v18`，保存草稿并确认发布。发布只影响之后创建的任务，不会改写历史任务。如果允许模式中没有 `qa`，需要在草稿 JSON 中加入 `qa`。

模型配置页面管理模型草稿、发布版本和探测任务；密钥只填写 `env://...` 等引用，实际环境变量必须在 Worker 中提供。发布模型版本后，需要在 Agent 草稿中选择该版本并发布，新的任务才会使用它。新建 Agent 不会自动获得模型凭据授权，部署管理员还需在 `service.model_credential_bindings` 中以 `agent_ids` 配置允许的 Agent、模型端点和凭据引用，并重启 Worker。

会话页面可新建会话或继续现有会话；任务记录页面展示所选 Agent 的真实任务。`dod.v18` 使用同一会话中符合校验的已完成 `dod.v17` 和 `dod.v18` 纯 LLM 问答作为历史，最多使用最近 8 轮，并受字节数和模型上下文预算限制。失败、未完成或不符合校验的记录不会进入模型上下文；包含其他工作流已完成记录的会话不能继续，请新建会话。

## 启用已有 Agent

使用管理员 Bearer Token 和已启动的 API。发布会改变 Agent 后续新任务的工作流，也会将允许的模式限制为 `qa`。如果其他模式仍需使用，请使用单独的 Agent。先检查现有草稿并与草稿负责人确认，再修改；不要覆盖他人的草稿。需要 `curl`、`jq` 和 `uuidgen`。

```sh
export WORKBENCH_API=http://127.0.0.1:8000
export WORKBENCH_AGENT_ID='<Agent UUID>'
export WORKBENCH_BEARER='<管理员 Bearer Token>'
qa_tmp=$(mktemp -d)
curl -fsS "$WORKBENCH_API/v1/agents/$WORKBENCH_AGENT_ID" -H "Authorization: Bearer $WORKBENCH_BEARER" > "$qa_tmp/agent.json"
jq '.draft' "$qa_tmp/agent.json"
jq '.draft | .graph_version="dod.v17" | .allowed_modes=["qa"]' "$qa_tmp/agent.json" > "$qa_tmp/draft.json"
jq . "$qa_tmp/draft.json"
curl -fsS -X PATCH "$WORKBENCH_API/v1/agents/$WORKBENCH_AGENT_ID" \
  -H "Authorization: Bearer $WORKBENCH_BEARER" -H 'Content-Type: application/json' \
  -H "If-Match: $(jq -r .revision "$qa_tmp/agent.json")" \
  -H "Idempotency-Key: $(uuidgen)" --data-binary @"$qa_tmp/draft.json" > "$qa_tmp/saved.json"
curl -fsS -X POST "$WORKBENCH_API/v1/agents/$WORKBENCH_AGENT_ID/profiles/publish" \
  -H "Authorization: Bearer $WORKBENCH_BEARER" -H "If-Match: $(jq -r .revision "$qa_tmp/saved.json")" \
  -H "Idempotency-Key: $(uuidgen)"
```

Agent 与模型端点必须获得 `service.model_credential_bindings` 授权。使用环境变量密钥时，引用也必须匹配，且变量须在 Worker 中可用；管理端加密密钥的配置见[本地启动手册](../../docs/runbooks/source-layout.md#管理端模型密钥)。无需 MCP 连接或知识工具。

构建前端后，重启 API、Worker 和 Scheduler，让它们加载新代码；各进程使用同一配置和数据库。打开 `/app/`，选择该 Agent 和“纯 LLM 问答”，提交问题。模型调用失败时任务显示部分完成，不会伪造答案。

```sh
cd "$WORKBENCH_ROOT/apps/web"
npm ci
npm run build
```

三个独立终端分别执行：

```sh
cd "$WORKBENCH_ROOT/apps/agent"
uv run workbench api --config "$WORKBENCH_ROOT/config/local/agent.yaml" --host 127.0.0.1 --port 8000
```

```sh
cd "$WORKBENCH_ROOT/apps/agent"
uv run workbench worker --config "$WORKBENCH_ROOT/config/local/agent.yaml"
```

```sh
cd "$WORKBENCH_ROOT/apps/agent"
uv run workbench scheduler --config "$WORKBENCH_ROOT/config/local/agent.yaml"
```

如果需要知识问答，则应选择发布了 `dod.v16` 的 Agent，并配置相应知识工具；详见采用同一 Agent 契约的 [知识问答配置](KNOWLEDGE_QA_SETUP.md)。

## 隔离浏览器测试

在 `apps/agent` 运行 `.venv/bin/python tests/support/qa_browser_harness.py --mode direct --port 8766`。
该测试使用临时数据库和模拟模型，启动独立 Worker，并在终端输出本地页面地址和测试令牌，不访问真实模型或知识源。退出后临时数据库会被删除。

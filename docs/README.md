---
title: 文档入口
status: stable
verified_commit: 008f681
---

# Workbench 文档

Workbench 是一个可自托管的 Agent 工作台：把业务答疑、告警 RCA、代码分析、SOP 与定时巡检放进同一套 Team → Agent → Thread → Run 协议里，任务可等待、可恢复、可追溯。

本目录是唯一的文档来源。根目录包含五篇主文档；原产品演进分析、知识管理设计和专项实施记录已并入其中。查表类内容在 `06-reference/`，图在 `assets/`。

## 快速开始

前置：Python 3.12、[uv](https://docs.astral.sh/uv/)、Node.js、SQLite ≥ 3.51.3。全部命令在仓库根目录执行。

```bash
# 1. 安装依赖、构建 Web、复制示例配置（不覆盖已有配置）
./scripts/install.sh

# 2. 首次建库：migrate → bootstrap → doctor（目标文件必须不存在）
./scripts/init-db.sh

# 3. 编辑本地配置（模型端点、存储、认证引用），密钥只由环境注入
$EDITOR config/local/agent.yaml

# 4. 启动 api / worker / scheduler（先 doctor，再起进程）
./scripts/start.sh

# 5. 打开 http://127.0.0.1:8000/app/
```

自定义数据目录：`./scripts/install.sh --data-dir /absolute/path`，再按 [04 运维 §1](04-operations.md#1-安装与首次初始化) 设置 `WORKBENCH_PERSISTENT_ROOT` 与 `WORKBENCH_DATABASE_URL`。

只有本地、没有模型凭据时，系统仍可启动并受理 Run，但模型调用会失败并给出明确错误；这不是"功能正常"。

运行测试：

```bash
(cd apps/agent && uv sync --frozen && uv run python -m pytest -q -ra --durations=25)
(cd apps/mcp-server && uv sync --frozen && uv run ruff check src tests && uv run ruff format --check src tests && uv run python -m pytest -q -ra)
(cd apps/web && npm ci && npm run typecheck && npm test && npm run build)
python3 -m unittest discover -s scripts/tests -v
python3 scripts/check_docs.py
```

以上脚本测试需要先完成 agent 的 `uv sync --frozen`。CI 和本地命令见 [03 开发 §9.3](03-development.md#93-持续集成与本地等价命令)。

首次建库、升级、备份恢复与容器部署的完整步骤见 [04 运维](04-operations.md)；代码入口与开发流程见 [03 开发](03-development.md)。

## 文档地图

| 文件 | 回答什么问题 | 读者 |
| --- | --- | --- |
| [01 产品](01-product.md) | 能做什么、给谁用、做到哪一步、还缺什么 | 产品、业务、管理员 |
| [02 架构](02-architecture.md) | 为什么这样设计、契约与边界在哪 | 架构师、评审人、核心开发 |
| [03 开发](03-development.md) | 代码在哪、一次请求怎么走、加功能怎么改 | 开发 |
| [04 运维](04-operations.md) | 怎么装、怎么起、怎么备份恢复、怎么排障 | 运维、值班 |
| [05 决策](05-decisions.md) | 每条关键取舍的背景、决策、代价、状态 | 架构师、新核心开发 |
| [06-01 接口](06-reference/01-api.md) | HTTP 路由、SSE 事件、MCP 工具与错误码 | 对接方、排障 |
| [06-02 数据字典](06-reference/02-data-dictionary.md) | 表、字段、约束、索引与迁移历史 | 查字段、写迁移 |
| [06-03 配置](06-reference/03-configuration.md) | 每个配置项的含义、默认值与引用方式 | 部署、配置 |
| [assets/](assets/) | Mermaid 源文件（`.mmd`）与渲染结果（`.svg`） | 改图的人 |

## 按角色阅读

- **第一次接触**：01 产品 §1–§3 → §4 能力状态 → README 快速开始。
- **要评审设计**：01 §7 非功能目标 → 02 架构全文 → 05 决策。
- **要改代码**：03 开发 §1–§3 → 对应实现章节 → 10 开发指引。
- **要部署或值班**：04 运维 §1–§3 → §10 排障速查。
- **只想知道状态**：01 产品 §4 能力清单 + §9 已知缺口。
- **要规划后续演进**：01 产品的演进与验收 → 02 架构的演进契约 → 03 开发的专项实现地图；能力状态仍以 01 产品为准。

## 文档约定

1. **编号即阅读顺序**：01 产品 → 02 架构 → 03 开发 → 04 运维 → 05 决策 → 06 查表。`README.md` 与 `assets/` 不编号。
2. **文件名**：小写短横线，数字前缀；不带日期、不带里程碑编号。新增文件顺延下一个编号，删除后**不复用**编号。
3. **顶部元信息**：每篇都有 `status: stable | draft` 与 `verified_commit`（最后一次对照源码的 commit）。状态为 `draft` 表示内容尚未对照当前源码。CI 要求它指向 HEAD 祖先，修改文档时更新；纯错别字 PR 可使用 `docs-typo` 标签跳过更新检查。
4. **一处一事实**：同一结论只写一处，其余用链接。能力状态只在 01 产品维护；决策论证只在 05 决策维护；字段、接口、配置以 06 参考为准。
5. **代码优先**：文档与源码不一致时以源码为准，并在该篇末尾的「核对记录」里记下差异。
6. **拆分原则**：按主题、职责与阅读成本拆分；不以行数作为告警阈值。当前 `01`–`06` 保持单文档，新增内容出现清晰独立职责时再评估拆分。

## 改文档时

| 你改了什么 | 必须同步 |
| --- | --- |
| 修改文档 | 更新该文档的 `verified_commit`（纯错别字除外） |
| 接口、配置、路由或脚本 | 检查 CI 的文档关联警告 |
| 能力上线或退化 | 01 §4 状态 + §9 已知缺口 |
| 设计契约、状态机、权限边界 | 02 架构 + 05 决策（新增 ADR，不改旧条目） |
| 路由、字段、配置项 | 06 参考对应文件 |
| 运维脚本、迁移、备份流程 | 04 运维 |
| 图的节点或命名 | `assets/*.mmd` 与同名 `.svg` 一起改 |

> 图必须以 `.mmd` 源文件为准。修改后重新渲染为 `.svg`（例如 `mmdc -i x.mmd -o x.svg`），两文件一起提交。

`assets/19-langchain-langgraph.svg` 暂无 Mermaid 源文件，门禁暂时豁免；补上源文件后移除 `SVG_WITHOUT_SOURCE` 条目。

# 原规格与当前源码差异清单

> 核对基线：`../2026-11-12-workbench-python-spec.md`（WORKBENCH-PY-SPEC-001 v1.9）与当前 `apps/agent`、`apps/mcp-server`、`apps/web` 工作树。本文记录影响产品状态、设计判断或实施路径的差异；逐字段、逐接口、逐配置项仍以[附录 A](03-implementation/A-data-dictionary.md)、[附录 B](03-implementation/B-api-reference.md)、[附录 C](03-implementation/C-config-reference.md)为准。

## 1 原文目标或表述有、源码尚未完整交付

| 项目 | 原规格位置 | 当前核对证据 | 结论与文档落点 |
| --- | --- | --- | --- |
| 真实 SeaTalk/告警入口 | §1.2 FR-01、§8.2 | `ingress/` 提供合成聊天/告警协议，`docs/acceptance/M3.md` 限定本地合成验收 | 产品 §4 标部分；不可当真实协议接入 |
| 工具连接、调度、事件、统计页面 | §20.2 | `apps/web/src/router.ts` 指向 `views/Placeholder.vue` | 页面未交付，后端接口单独计状态 |
| Web 取消、补充输入、审批与流式正文 | §1.2 FR-10、§20.5 | 后端路由存在；实际视图仍缺对应完整交互，`sse.ts`/`run-stream.ts` 主要是客户端基础模块 | 产品 §4、§8 标部分 |
| 知识问答完整结论 | §7.5、§23 | `runtime/graphs/qa_v16.py` 和验收记录说明来源摘录与 partial 边界 | 产品标部分，不把摘录称为完成答复 |
| 真实监控/日志/Trace 基线 | §7.6、§8.2 | `runtime/graphs/rca_baseline.py` 有基线流程；验收仍为合成数据 | 需要真实源连接与质量验收 |
| 多专家并行调查、假设合成 | §7.4、§8.2 | `runtime/graphs/multi.py` 顺序执行；M3 验收说明并行待实现 | 技术设计区分当前拓扑与目标拓扑 |
| SOP 有限并行 | §1.2 FR-09、§7.7 | `runtime/graphs/sop_graph.py` 按拓扑顺序串行 | 当前实现为 DAG 定义、串行执行 |
| Skill 引用资料与脚本自动加载 | §9.1–9.2 | `runtime/graphs/conversation_qa.py` 使用显式绑定 `SKILL.md` 文本，`skill_management/loader.py` 支持受控按资源读取但图未通用接入 | 产品状态按用户可用路径标部分 |
| MySQL/PostgreSQL 后端 | §3.5、§21.3 | `persistence/backends/sqlite.py` 为当前实现；无对应生产适配器 | 技术设计是迁移契约，实施文档写明缺口 |
| Prometheus/Grafana 与生产监控 | §19.2、§22 | 结构化日志/Trace 代码存在，指标与生产规则未交付 | 不把设计目标写成实测 SLO |
| 真实 Jira/SeaTalk/DoD 外部发送器 | §10.2、§16 | `delivery/local_simulator.py` 为本地模拟，真实发送器默认未配置 | 产品标部分，Action 与 Run 终态分开 |
| 通用长期记忆 | §1.3、§7.5 | v18 仅有界加载同会话已完成历史；无长期记忆机制 | 保留为非目标或后续扩展 |

## 2 源码已有、原规格或拆分草稿需要补写

| 项目 | 源码入口 | 本次处理 |
| --- | --- | --- |
| 独立 MCP Server 的四层实现 | `apps/mcp-server/src/workbench_mcp_server/{interfaces,application,domain,infrastructure}` | 实现文档 §7 补进程、请求链、修改路径 |
| Agent 独立 Schema 与安全迁移边界 | `persistence/schema_agent_v3.py`、`agent_migrations/`、`cli/db.py` | 技术设计 §13、实现文档 §5、附录 A 统一 Agent 链口径 |
| 共享 Skill 草稿、发布与资源校验 | `skill_management/{library,manifest,loader}.py` | 技术设计 §7、实现文档 §6 补全过程 |
| 管理端模型密钥的受控存储 | `connectors/managed_model_secrets.py`、`docs/runbooks/source-layout.md` | 实现文档 §9 链接配置与部署说明，不在产品文档暴露密钥操作细节 |
| Web 真实路由与占位路由区分 | `apps/web/src/router.ts` | 产品 §4、实现文档 §8 分别标状态 |
| Web SSE 投影的 attempt/message_id 语义 | `apps/web/src/{sse,run-stream}.ts` | 实现文档 §8 补事件消费与排障路径 |
| `workbench` CLI、安装与初始化脚本 | `apps/agent/pyproject.toml`、`scripts/{install,init-db,start}.sh` | 实现文档 §2、§9 统一命令与维护边界 |
| `check` 不能证明 MCP 上游可查 | `apps/mcp-server/src/workbench_mcp_server/interfaces/cli.py` | 实现文档 §7 写明输出 `business_query: not_checked` |
| 旧 Worker 晚写与回复重放的故障测试 | `apps/agent/tests/fault/{test_guard_takeover_race,test_resume_replay,test_interrupt_projection}.py` | 技术设计 §10–11、实现文档 §10 对应验收 |

## 3 术语与历史资料

- 当前产品实体是 Team → Agent → Thread → Run。`Workspace` 仅出现在旧库、迁移历史、旧图快照或 Runner 技术运行目录中，不是当前产品对象。
- 原规格和少数旧 runbook 中的 `dod` 命令应以 `apps/agent/pyproject.toml` 的 `workbench` 入口为准。旧文档需要另行清理，不能把其命令复制到新的实施说明。
- `docs/assets/workbench-python/` 的部分 ER 图仍含旧 Workspace 命名。当前关系以 Agent Schema 和[数据字典](03-implementation/A-data-dictionary.md)为准；图注只用于历史理解。
- `docs/acceptance/` 中的合成与真实服务验收范围不同。产品文档使用“已实现 / 部分 / 规划”时，以用户可实际完成的场景及其目标环境为判断口径。

## 4 本次核对范围与后续维护

本清单比较规格目标、当前入口、主要运行链与交付状态；它不是逐行源码审计。新增路由、表、迁移或配置时，同时更新对应附录；改变 Run 生命周期、权限或恢复契约时，先更新技术设计与故障测试。仓库当前另有 `scripts/tests/test_local_workflow.py` 的未提交改动，本次文档工作不修改该文件。

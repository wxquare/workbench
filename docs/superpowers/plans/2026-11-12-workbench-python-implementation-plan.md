---
title: "DoD Agents Python：分阶段执行计划"
date: "2026-11-12"
timezone: Asia/Shanghai
status: in-progress
spec_id: DOD-PY-SPEC-001
spec_version: "1.3"
---

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. 如果用户选择委派执行，再使用 superpowers:subagent-driven-development；本计划不自动启动实现或委派。

**Goal:** 按 DOD-PY-SPEC-001 v1.3 实现可持久化、可恢复、有证据和外部动作台账的 DoD Python 系统，先交付 SQLite 首版，再独立实现服务型数据库与跨库迁移。

**Architecture:** 一个模块化 Python 项目，API、Worker、Scheduler、Dispatcher、Runner 分进程。LangGraph 驱动状态流程，LangChain 执行专家循环；所有业务记录、Job、Guard、检查点、预算与 Outbox 使用同一 StorageBackend。外部调用位于数据库短事务之外，副作用通过稳定 Action 标识与对账完成。

**Tech Stack:** Python 3.12、FastAPI、Pydantic v2、LangChain 1.x、LangGraph 1.x、langchain-deepseek、SQLAlchemy 2 async、Alembic、SQLite 3.51.3+、aiosqlite、uv、S3 兼容对象存储、可选 Redis；后续 asyncmy / psycopg。

**规格来源：** [技术设计规格](../../../2026-11-12-workbench-python-spec.md)。本计划采用规格 v1.3 的源码与资产目录契约；“章节”表示规格章节号。工作目录统一为 `/Users/xianguiwang/Projects/wxquare-private/projects/dod-agent`。规格示例中的 `dod-agent/` 即项目根，不再创建额外嵌套目录。

**当前事实（2026-11-12）：** M1 运行底座已实现并通过任务复审、整个分支复审及主流程独立完整验证（416 项测试通过）。详见 [M1 实施计划](2026-11-12-workbench-python-m1-foundation.md)和 [M1 验收记录](../../acceptance/M1.md)。M2–M5 尚未执行，其文件、命令和通过条件仍为后续交付计划。当前迁移头为 `0005_resume_protocol`；后续迁移从实际迁移头向前，计划中的示例编号不得用于覆盖已有迁移。

## Global Constraints

以下是每个阶段共同遵守的规格约束；各阶段增加约束时不得降低这些要求。

- 版本基线：`Python 3.12`、`Pydantic v2`、`LangChain 1.x`、`LangGraph 1.x`、`SQLAlchemy 2 async`、`SQLite 3.51.3+`；后续 `MySQL 8.4 LTS / PostgreSQL 16+`。依赖提交 `uv.lock`，升级重新验收恢复兼容。
- 首版为同一主机本地持久磁盘、同一数据库绝对路径；一个 Worker 实例、2 个 Run 并发，单团队最多 2 个活跃执行；专家最多 2、工具并发最多 4。不在 NFS 上共享 SQLite。
- SQLite 连接：`foreign_keys=ON`、`busy_timeout=5000`、`synchronous=FULL`；初始化确认 `journal_mode=WAL`、`wal_autocheckpoint=1000`。存储操作总期限默认 5 秒；不能以增加重试无限延长。
- 业务、Job、Lease、Checkpoint、Event、Budget、Outbox 同后端同数据库。Guard 与受保护写入共用同一 UoW、连接及事务；不使用 Redis 锁代替持久化互斥。
- 写事务显式 `BEGIN IMMEDIATE`；模型、MCP、Git、脚本、对象上传、SSE 等待均在事务外。普通服务启动不执行 DDL。
- UUID 为规范化小写 36 字符字符串；时间为 UTC 毫秒 BIGINT；JSON 为规范化 UTF-8 TEXT；金额为整数微单位与 currency。公共查询不依赖 JSONB、原生 UUID/Enum、自增序列、条件唯一索引或方言 upsert。
- 业务 `thread_id` 与图 `configurable.thread_id` 区分，后者为 `run_id`。同业务 Thread 至多一个非排队活动 Run，waiting 仍占逻辑位置，但释放 Worker 槽位与执行额度。
- Run 固定 Workspace、模型、Graph、Skill、工具和仓库版本；恢复不换最新版。权限撤销每次执行前即时生效；Scope 改动创建新版本，旧范围证据不能充当新范围正常结果。
- 租约 45 秒、每 10 秒续期、每 5 秒扫描；成功领取/接管同事务递增 epoch。无所有权扫描不得失效旧执行；`STALE_EXECUTION` 不自动重试。
- 统一 Saver 实现 `aget_tuple`、`alist`、`aput`、`aput_writes`、`adelete_thread`，保留 typed bytes、父链、namespace、pending writes、Codec 版本及锁定框架的特殊 write index 语义。
- API `/v1`；`Idempotency-Key` 长度 1–128，按主体、Workspace、路由、键保留 72 小时。同键同载荷重放原响应，同键异载荷 409。资源跨团队与不存在统一 404。
- 创建 Run 同事务保存 Message、Run、Job、首事件与幂等响应，提交后才返回 202。恢复通过 Interrupt CAS 指针选定唯一 ResumeRequest。
- SSE `id=run_id:seq`，先持久化后发送；250 毫秒或 256 字符合并、15 秒心跳、Redis 失效最多 1 秒轮询、缓冲最多 100 条、事件保留 7 天；批量读取结束关闭读事务。
- DeepSeek 首版默认 `deepseek-dod-default@1`；endpoint `https://api.deepseek.com`、示例 model_id `deepseek-flash`，均版本化可配置；`thinking.type=disabled`；密钥仅 `env://DEEPSEEK_API_KEY` 等 secret_ref。
- 配置种子 `create_if_missing`；数据库发布版本为运行权威。角色绑定优先于 Workspace 默认，部署默认只用于新建；不得用 YAML 重启、Skill 或用户正文改变已发布绑定。
- `sdk_max_retries=0`，ModelInvoker 最多 3 次总尝试；JSON Output + Pydantic，空白/截断/不符 Schema 不能成功，最多一次预算内修正；无自动换模型。
- 预算 qa：180 秒、8 次模型、20 次工具、80,000 Token；rca：300 秒、12/30/120,000；code：600 秒、12/30/120,000；sop/inspection：600 秒、12/40/120,000。专家、评估、真实重试共用父预算；未知 usage/cost 为 pending。
- 等待输入默认 24 小时，Run 总墙钟 48 小时，确认默认 30 分钟；有有效未完成 Outcome 才可 partial，无 Outcome 的硬超时为 timed_out。
- Runner 默认 2 vCPU、2 GiB、60 秒/步骤、20 MiB/单文件；argv 数组、固定 commit/镜像 digest、目录与符号链接越界检查、独立进程组、取消终止整个组。Token 不进入共享 os.environ。
- 输入默认不超过 64 KiB，单次持久化图状态默认不超过 2 MiB；大证据和产物对象化，上传及摘要验证后才 available。
- Action 在发请求前持久化 action_id 与参数哈希。确认绑定 tool_version、目标、参数、策略、主体和期限；结果不确定为 unknown，只能可靠查询/人工对账。
- Outbox 最多 8 次尝试，间隔 2 秒、10 秒、30 秒、2 分钟、10 分钟、30 分钟、2 小时。Run 分析终态与动作/通知回执独立。
- 定时触发 `schedule_id+due_at` 唯一，IANA 时区、UTC due_at，默认 `overlap_policy=forbid`；错过 6 小时内合并最近一次，超窗记录 missed；每次独立 Thread。
- SQLite 备份使用 Online Backup API 或 VACUUM INTO；恢复先校验完整性、外键、检查点与动作对账，再开放外部写入。
- 不记录或展示模型内部思考，不输出凭据；外部观测前脱敏。Prometheus 不使用 run_id 等高基数标签。
- 保留期严格按规格 章节 13.4：Run/摘要/费用 90 天、Duty Event 180 天、原始证据 7 天、普通产物 90 天、来源去重 90 天、审计 180 天、终态检查点 30 天；有可恢复 Run 引用的发布版本不能删除。

## 阶段和任务依赖

先交付一条可运行的纵向链路，再扩大能力。M1 固定响应图只证明运行底座；M2 起必须使用真实 LangChain/LangGraph、模型与业务连接器。

| 阶段 | 任务 | 可演示交付 | 完成门槛 | 详细计划 |
| --- | --- | --- | --- | --- |
| M1 运行底座 | T01–T09 | API 受理 → 持久化 Job → 图 → 中断/恢复/取消 → SSE/结果 → 进程故障恢复 | 真 SQLite 文件与跨进程故障验证 | [M1](2026-11-12-workbench-python-m1-foundation.md) |
| M2 能力执行 | T10–T15 | DeepSeek + MCP/知识 + Skill/固定仓库/Runner + 三类专家、预算与用量 | 真实模型/知识/Runner 可调用，失败正确表达 | [M2](2026-11-12-workbench-python-m2-capabilities.md) |
| M3 DoD 链路 | T16–T20 | SeaTalk/告警 → RCA/SOP/巡检 → 人工输入/确认 → Duty Event/DoD/Jira/通知 | 真实监控、日志、Trace、值班、Jira、SeaTalk 验收 | [M3](2026-11-12-workbench-python-m3-dod-workflows.md) |
| M4 运营与发布 | T21–T24 | 管理页面、人工标签评估、资产迁移、灰度与回滚、容量及备份演练 | AT 首版范围及质量/容量发布门槛全部通过 | [M4](2026-11-12-workbench-python-m4-operations-release.md) |
| M5 后续存储 | T25–T27 | MySQL/PostgreSQL 适配、停写逻辑迁移与恢复 | AT-30/31 与同一共享契约集 | [M5](2026-11-12-workbench-python-m5-storage-portability.md) |

默认顺序为 T01 → T02 → T03 → T04 → T05 → T06 → T07 → T08 → T09 → T10 → T11 → T12 → T13 → T14 → T15 → T16 → T17 → T18 → T19 → T20 → T21 → T22 → T23 → T24；M5 在 M4 完成后单独安排。任务标出的局部依赖用于理解接口，不授权并发委派。没有团队人数、现有容量和外部连接开通日期，故不虚构工期或承诺日历日期。

## 实施文件边界

以下均相对项目根目录；具体文件在分阶段任务中明确。

| 路径 | 责任 |
| --- | --- |
| `src/api/`、`ingress/`、`application/` | HTTP/OIDC/SSE、入口规范化、用例事务与 DTO |
| `src/domain/` | 状态机、不可变身份/版本/Scope、Evidence、Outcome、动作和权限 |
| `src/persistence/ports/`、`backends/`、`checkpoints/`、`migrations/`、`objects/` | 存储 Protocol、方言、Codec/检查点物理表、迁移和对象存储 |
| `src/jobs/`、`runtime/checkpoints/`、`runtime/state/` | 原子调度、租约、Guard、Saver、恢复与图状态 |
| `src/runtime/models/`、`budgets/`、`experts/`、`graphs/` | 统一模型调用、共享预算、专家和场景图 |
| `src/skill_management/`、`src/workspaces/`、`src/tools/`、`src/connectors/` | Skill/Workspace 管理代码、执行策略和外部协议适配；资源包位于根目录 |
| `src/incidents/`、`delivery/`、`evaluation/`、`observability/` | 业务事件、Action/Outbox、效果及用量、日志/指标/Trace |
| `src/cli/`、`runner/`、`deploy/`、`config/` | 六类进程入口、执行镜像、部署配置、初始化种子 |
| `src/web/` | FastAPI 提供 SPA 静态资源和 /app 客户端路由回退；业务只走 /v1 授权接口 |
| `skills/`、`prompts/`、`config/workflows/` | Skill 包、提示模板和版本化工作流资源；不参与 Python 包发现 |
| `web/` | Vue 3、TypeScript、Vite、Element Plus、Vue Router 前端源码；构建后将 `web/dist/` 复制到资源根目录 |
| `runner/`、`var/` | Runner 协议/镜像资源与本地运行数据；var 不纳入版本管理 |
| `tests/unit/`、`contract/`、`integration/`、`fault/`、`e2e/`、`capacity/` | 纯逻辑、协议、真实数据库、故障、业务和容量验收 |
| `docs/acceptance/`、`docs/runbooks/` | 版本化验收记录、恢复/发布/对账操作手册 |

UI 技术栈和测试目录是本计划的实现选择，不修改规格外部协议；如果已有实现出现，先核对目录再调整对应计划，不能重建另一套项目。

## 源码与资产路径约定

`src/` 下直接放置 Python 包和模块，代码导入不含项目同名包层。Skill 管理逻辑为 `src/skill_management/`，实际资产为根目录 `skills/`；Prompt、工作流和 Web 资源分别为 `prompts/`、`config/workflows/`、`web/`。包发现配置 `where = ["src"]`、`namespaces = false`，独立模块为 `settings`，命令入口为 `dod = "cli.main:main"`。T01 必须在项目外验证安装 wheel 后的导入与 CLI，T13/T14/T22 分别验证 Skill、Runner 和 Web 资源从 `WORKBENCH_ASSET_ROOT` 加载；不依赖工作目录或 `PYTHONPATH`。

## 测试与提交规则

- 每个任务依次执行：写有业务含义的失败测试 → 确认失败原因 → 实现最小纵向交付 → 运行任务及受影响契约 → 审查差异后提交。每个勾选步骤为一次小动作；列出的多个失败场景分别循环，不把整阶段作为单次实现。
- 分阶段文件中的 Python 代码是关键测试和关键算法示例；同任务列出的矩阵用例也必须实现。不是把示例粘贴后即视为整个任务完成。
- 普通命令在项目根目录运行，统一 `uv run pytest`。T01 首次同步后方可使用；真实连接测试加 `--run-live`，默认不产生模型费用或外部写入。
- `tests/conftest.py` 设置 pytest-asyncio auto 与 markers。`client` 为 ASGITransport AsyncClient；`platform` 是测试组合根，返回实际服务、数据库和只在测试使用的受控模型/连接器。纯单元替身只用于可控错误与竞争时序，不能充当真实业务验收。
- 所有状态集成测试使用 `tmp_path` 下的真实 SQLite 文件；临时目录和内存库只用于测试，不作为 doctor 允许的正式配置。并发测试使用独立连接/进程和 Barrier，不用固定 sleep 猜顺序。
- 新增测试夹具必须在对应任务列出的 `tests/support/` 文件实现，直接组合生产服务；不创建另一套业务引擎或靠改库伪造经过服务验证的成功结果。
- Git 根是上层 `wxquare-private`。若需隔离，在执行时按 using-git-worktrees 检查原生 worktree 能力；当前只写计划，不建分支/提交。实施提交只 stage 当前任务显式路径，禁止 `git add .` 混入其他项目内容。
- 每项任务的提交消息在分阶段计划中给出；提交前 `git diff --check`，检查 staged diff，再运行该任务验证。失败保留证据，不推进完成标记。

## 外部依赖与真实验收准备

T01 起登记 `docs/acceptance/connector-readiness.md`：知识、监控、日志、Trace、Git、DoD、Jira、SeaTalk、DeepSeek、S3；记录 connector/binding 版本、环境、团队范围、负责人、只含 secret_ref 的配置、最近实际调用与证据链接。可以先用本地回归推进；缺少真实连通记录时相应 AT 保持未通过。

M2 真模型 probe 使用已授权测试配置；M3 Jira/通知只向已授权测试项目/渠道写入。测试和正式验收分别使用对应环境，不从本计划中推断任何新的生产操作权限。密钥经部署注入，不能写入文档、测试数据或日志。

初始 30 个案例的数据准备从 T12 开始：正常问答 6、已人工确认根因的告警 10、无数据 4、证据冲突 4、需要人工 3、预算不足 3；用例附权限和工具可用性标签，至少覆盖越权拒绝与工具不可用不能报正常。预期根因、引用和可用性标签由领域人员审核；生成样例不能冒充人工标签。

## 验收覆盖矩阵

“首次落地”是关键机制的任务；“发布复验”统一为 T24。M5 不纳入 SQLite 首版完成声明。

| 用例 | 首次落地任务 | 测试文件或记录 |
| --- | --- | --- |
| AT-01 | T04 | `tests/integration/test_run_admission.py` |
| AT-02 | T16、T20 | `tests/e2e/test_seatalk_conversation.py` |
| AT-03 | T03、T08、T12、T22 | `tests/integration/test_tenant_isolation.py` |
| AT-04 | T13 | `tests/contract/live/test_knowledge.py` |
| AT-05 | T17、T20 | `tests/e2e/test_alert_rca_live.py` |
| AT-06 | T07、T17 | `tests/integration/test_query_scope.py` |
| AT-07 | T10、T12、T15 | `tests/integration/test_expert_budget.py` |
| AT-08 | T13、T14 | `tests/e2e/test_skill_runner.py` |
| AT-09 | T14 | `tests/contract/test_runner_isolation.py` |
| AT-10 | T18 | `tests/integration/test_sop.py` |
| AT-11 | T18 | `tests/integration/test_schedules.py` |
| AT-12 | T05、T06、T09 | `tests/fault/test_worker_recovery.py` |
| AT-13 | T07、T09 | `tests/fault/test_interrupt_projection.py` |
| AT-14 | T07、T09 | `tests/fault/test_resume_replay.py` |
| AT-15 | T08 | `tests/integration/test_sse_replay.py` |
| AT-16 | T07、T09 | `tests/fault/test_cancel_race.py` |
| AT-17 | T19、T20 | `tests/e2e/test_delivery_live.py` |
| AT-18 | T19 | `tests/fault/test_action_unknown.py` |
| AT-19 | T10、T11、T21 | `tests/integration/test_usage.py` |
| AT-20 | T21 | `tests/integration/test_feedback_evaluation.py` |
| AT-21 | T04、T08、T09、T19 | `tests/fault/test_redis_outage.py` |
| AT-22 | T13、T23 | `tests/fault/test_version_retention.py` |
| AT-23 | T05、T06 | `tests/contract/test_execution_guard.py` |
| AT-24 | T23 | `tests/integration/test_engine_cutover.py` |
| AT-25 | T06、T09 | `tests/fault/test_sqlite_restart.py` |
| AT-26 | T02、T05、T07、T10 | `tests/fault/test_sqlite_contention.py` |
| AT-27 | T05、T06 | `tests/fault/test_guard_takeover_race.py` |
| AT-28 | T09、T24 | `tests/fault/test_backup_restore.py` |
| AT-29 | T08、T24 | `tests/integration/test_sse_wal.py` |
| AT-30 | T25、T26 | `tests/contract/storage/`，三后端同集 |
| AT-31 | T27 | `tests/integration/test_storage_transfer.py` |
| AT-32 | T01、T02 | `tests/integration/test_storage_readiness.py` |
| AT-33 | T03、T11 | `tests/integration/test_model_bootstrap.py` |
| AT-34 | T03、T11、T15 | `tests/integration/test_model_binding.py` |
| AT-35 | T11、T15 | `tests/contract/live/test_deepseek.py` |
| AT-36 | T11、T21 | `tests/contract/test_model_errors.py` |

FR 映射：FR-01→T04/T16；FR-02→T03/T11/T14/T22；FR-03→T13/T15/T16；FR-04→T17；FR-05→T15；FR-06→T13；FR-07→T14；FR-08→T12；FR-09→T18；FR-10→T04–T09；FR-11→T19/T20；FR-12→T10/T21/T22；FR-13→T02/T05/T06/T09，后续 T25–T27。

## 发布门槛与执行记录

每阶段生成 `docs/acceptance/Mn.md`，记录 commit/lock/Schema/Codec/Graph/镜像版本、命令、环境、实际结果、日志或产物摘要、未通过项与原因。命令通过后才勾选；真实连接缺失、跳过、xfail 或仅 mock 不得登记为通过。

M4 完成需同时满足：

- AT-01–29、AT-32–36 通过；AT-30/31 明确列为 M5，不冒充三后端可用。
- 至少 30 个带人工标签案例，至少 10 个根因确认告警；证据引用正确率 ≥95%，人工认可可用答复 ≥80%，根因匹配 ≥80%；“工具不可用却输出正常”“越权访问”漏检为 0。
- 低竞争、本地持久磁盘条件下受理 p95 ≤500 毫秒；1 Worker/2 Run 并发与团队公平性有效；数据库与下游可用时 Worker 故障 90 秒内安排恢复。
- 完成 SQLite 一致备份/恢复、对象引用校验和 unknown 动作对账演练，确认后再开放目标环境外部写能力。
- API/OpenAPI、配置、迁移、进程命令和运维手册同版；UI 能显示真实过程、证据、费用、未完成项与独立投递状态。

执行入口：[M1 / T01](2026-11-12-workbench-python-m1-foundation.md)。当前为计划交付，所有实施复选框保持未勾选。

## 规格落地补全记录

实施前已有设计，不重新选择架构。为让文档中的持久化契约可实现，本计划显式补全以下存储对象；均遵守同库、授权、版本和迁移规则：

| 对象 | 规格依据 | 任务与边界 |
| --- | --- | --- |
| query_scope_revisions | 5.2的QueryScopeRevision/证据Scope引用 | T02建表、T17接通可信范围修订，不允许模型直接修改 |
| workflow_versions | 7.3的WorkflowVersion | T18独立Schema迁移与DAG校验，不增加可视化编排范围 |
| admin_jobs/admin_job_events | 11.2要求Skill导入、仓库sync、probe为持久化后台Job | T02/T05提供管理任务CAS/租约；不占用业务Thread位置，不与Run Guard混用 |
| migration_routes/import_records | 21.1/21.2资产来源、去重和切流记录 | T23唯一入口分配engine，不能靠两系统分别去重宣称唯一执行 |

后续计划中的Alembic文件编号按执行时已存在链顺延；不得改写已发布revision。规格 Markdown、HTML、ZIP 与本计划同步使用 v1.3 目录契约。

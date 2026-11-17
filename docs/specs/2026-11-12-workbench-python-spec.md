---
title: 我的工作台：多 Agent 平台定位、技术设计与实现边界
---

| 项目 | 内容 |
| --- | --- |
| 文档标识 | WORKBENCH-PY-SPEC-001 |
| 版本 | 1.9 |
| 日期 | 初版 2026-11-12；更新 2026-11-17，Asia/Shanghai |
| 状态 | 持续更新的技术设计；部分能力已实现并经本地验证，完整业务验收待完成 |
| 实现语言 | Python 3.12 |
| 核心框架 | FastAPI、LangChain、LangGraph |
| 数据库策略 | 首版 SQLite，可替换为 MySQL 或 PostgreSQL |
| 首版 LLM | DeepSeek，默认配置 deepseek-dod-default，示例模型 deepseek-flash |
| 设计范围 | 以“我的工作台”承载可配置 Agent、任务和会话；DoD 是重点业务场景 |
| 交付边界 | 主要工作台页面、直接 LLM 问答、团队 Skills 管理与发布、Agent 添加技能已实现；其余管理页面、真实 DoD 业务连接与生产部署验收仍需完成 |

产品面向用户的名称为“我的工作台”：用户选择具体 Agent 发起任务，在任务记录中查看执行结果，在会话中连续追问；管理员维护 Agent 的工作流、模型、系统提示词、回复偏好、上下文与预算。DoD Agent 是其中面向值班答疑、告警诊断和跟进的业务配置，不再作为所有页面的统一产品名称。

当前可用主链路是“选择 Agent → 提交问题 → API 持久化 Run → Worker 调用 LLM → 保存结果 → 工作台展示”，多轮问答还会读取同会话内经过授权的有界历史。它不依赖知识源即可运行，也不因此具备实时监控、知识检索或业务操作能力。面向完整 DoD 场景，目标仍是接收告警、业务问题及定时任务，结合知识、Skill、监控、日志、Trace、代码和业务工具形成证据、事件及跟进动作。

系统采用同一项目内的多应用结构：Python Agent 提供 API、Worker、Scheduler，Vue 工作台单独构建，MCP Server 可作为独立服务运行。Dispatcher 和 Runner 是目标架构中的执行职责，其实际运行入口与边界见第 4、18 节。LangGraph 负责流程和执行状态，LangChain 负责模型与工具循环；StorageBackend 保存任务、检查点、事件和业务记录，首版使用 SQLite，后续适配 MySQL/PostgreSQL；Redis 用于通知、缓存和限流加速，对象存储保存文件产物。任务受理和恢复不依赖进程内存。

文中的 SHALL 表示首版必须满足的目标契约，SHOULD 表示推荐行为。性能数值是验收目标，不代表已经达到。原有图表展示目标架构和业务流程，其中独立 Dispatcher、Runner、服务型数据库及真实外部连接不应视为现成部署；当前可运行范围以第 2、4、18、20 节及验收记录为准。Markdown 为持续维护版本，同目录的 HTML 和 ZIP 为原始历史快照，未随本次更新重新生成。

**Agent 领域契约：**“我的工作台”承载多个独立可配置的 Agent，DoD Agent 只是其中一种场景；产品与领域均不再设置 Workspace 层。第 5、11、13、18、20 节与源码统一采用新契约：`agents`、`agent_id`、`agent_scope`、`agent_profile`、`/v1/agents` 与 `/app/agents`。本次验证状态与部署边界见第 21.4 节；既有历史验收不能作为新契约通过的证明。Runner 的 `/workspace` 仅是容器运行目录，LangChain 专家仅是工作流内部角色，均不是产品实体。

## 目录

1. [目标和核心范围](#1-目标和核心范围)
2. [现有系统与能力对应](#2-现有系统与能力对应)
3. [架构决策与整体架构](#3-架构决策与整体架构)
4. [模块组成和部署单元](#4-模块组成和部署单元)
5. [领域对象与身份](#5-领域对象与身份)
6. [启动与关闭](#6-启动与关闭)
7. [Agent 与工作流设计](#7-agent-与工作流设计)
8. [运行时序](#8-运行时序)
9. [Skill 知识与 Agent](#9-skill-知识与-agent)
10. [工具与模型适配](#10-工具与模型适配)
11. [核心对外接口](#11-核心对外接口)
12. [内部接口与事件](#12-内部接口与事件)
13. [存储设计](#13-存储设计)
14. [状态机](#14-状态机)
15. [调度并发与一致性](#15-调度并发与一致性)
16. [容错与恢复](#16-容错与恢复)
17. [权限与操作确认](#17-权限与操作确认)
18. [配置与运行契约](#18-配置与运行契约)
19. [可观测性和评估](#19-可观测性和评估)
20. [我的工作台与管理端](#20-我的工作台与管理端)
21. [迁移与发布](#21-迁移与发布)
22. [容量和服务目标](#22-容量和服务目标)
23. [验收用例与完成标准](#23-验收用例与完成标准)
24. [交付顺序与决策记录](#24-交付顺序与决策记录)
25. [参考资料与图源](#25-参考资料与图源)

## 1 目标和核心范围

### 1.1 目标

- 产品主线：以 Agent 为使用与配置入口，以 Thread 承载会话，以 Run 承载每次独立执行；任务结果可查、配置版本可追溯。
- G1：业务答疑、告警 RCA、代码分析、SOP 和定时巡检使用同一套任务与会话协议。
- G2：在 Python 服务中实现 LangGraph 工作流及 LangChain Agent 循环，支持多模型与可替换工具。
- G3：将现有 Skill、知识、仓库和工具资产接入新的运行时，具备运行脚本与管理产物的能力。
- G4：任务在客户端断线、Worker 重启、重复请求和工具失败后仍保持可解释的状态。
- G5：分析结论包含证据、影响和未完成项，可进入事件、DoD 跟进、Jira 及通知流程。
- G6：团队、用户、独立 Agent、运行配置和工具权限可以管理，运行成本和效果可以追踪。
- G7：业务逻辑、任务、事件与图检查点共同支持可替换的数据库后端；首版使用 SQLite，不依赖 PostgreSQL 专用特性。

### 1.2 P0 功能边界

| 编号 | 能力 | P0 交付与验收边界 |
| --- | --- | --- |
| FR-01 | 统一入口 | Web/API、SeaTalk 消息及告警 Webhook；支持签名验证、消息去重与线程关联 |
| FR-02 | 团队和 Agent 管理 | 团队成员/角色、Agent 草稿与发布、工作流与模型版本、系统提示词、回复偏好、上下文和预算；仓库、Skill、工具按工作流能力绑定，执行镜像按部署/Runner 契约管理 |
| FR-03 | 单轮与多轮答疑 | 直接 LLM 问答与有界历史连续追问；完整 DoD 目标另包括知识问答、业务工具查询、证据引用和不确定性说明 |
| FR-04 | 告警诊断 | 规范化告警、固定时间锚点、历史事件查询、监控/日志/Trace 与按需代码分析 |
| FR-05 | Agent 协作 | DoD 主图与 Knowledge、Diagnosis、Code 三类专家子图，按需调用且共用预算 |
| FR-06 | Skill 和知识 | Skill 导入、校验、版本、发布/回滚、按需加载、知识检索和来源记录 |
| FR-07 | Agent 执行 | Git 仓库快照、文件读取/搜索、Skill 脚本执行、受控运行环境和文件产物 |
| FR-08 | 工具 | MCP 发现/调用、Schema 校验、身份注入、错误分类、超时与执行台账 |
| FR-09 | SOP 和巡检 | 版本化固定工作流、步骤变量、分支、有限并行、定时触发与手动触发 |
| FR-10 | 任务生命周期 | 排队、异步、SSE、取消、等待输入/确认、恢复与最终结果持久化 |
| FR-11 | 业务结果 | Duty Event、DoD 值班人查询/跟进、Jira 创建或关联、SeaTalk 结果通知 |
| FR-12 | 运营与评估 | Run 详情、模型/工具用量、用户反馈、知识缺口记录与基础统计 |
| FR-13 | 可替换存储 | P0 实现 SQLite 与统一存储/检查点契约；MySQL/PostgreSQL 后续适配，迁移保留 Run、检查点和幂等记录 |

上表为完整 DoD P0 目标，不代表本版本全部完成。当前工作台增量先交付任务记录、Agent 管理、模型配置和会话，直接 LLM 问答可以单独验收，不以知识源接入为前置条件。完整 DoD P0 仍要求至少接通一套真实知识源、监控/日志源和代码仓库，形成端到端诊断链路；Trace、值班与 Jira 连接器也须实际连通验收。测试替身不能代替业务连接器验收。

### 1.3 后续扩展

MySQL/PostgreSQL 适配器与跨后端迁移工具在后续实现，并遵循本 Spec 的统一契约；首版完成 SQLite 的实际运行验收。完整的历史周报页面、自动 Skill 生成/优化、复杂 TD 文档生成、任意动态专家编队、跨团队 NOC 父事件和自动变更处置属于后续扩展。P0 保留统一 Run、Artifact、SkillVersion 和工作流注册接口，供这些能力复用。

P0 的代码能力包括仓库检索、实现分析及执行 Skill 所需脚本；生产变更通过明确注册的业务动作接入，不由通用 Shell 任意实施。

## 2 现有系统与能力对应

### 2.1 源码基线

初版设计参考本地两个历史仓库。ai-planner 的 Go 业务编排与 MCP 能力，以及 ai-agents 的 Node 执行封装构成参考功能基线。现有主链路使用 Smart NewReACT 与 Claude 两种不同执行路径：前者调用 Shopee Smart API，后者调用 ai-agents 的 Claude Agent SDK 服务。[S1][S2][S3]

ai-agents 当前任务和流事件缓冲使用内存 Map，Workspace 和部分费用记录在本地文件系统。因此，新系统将持久化任务、会话映射和事件回放作为统一底座。[S4][S5]

### 2.2 能力迁移表

| 现有能力 | 参考位置 | 新模块 |
| --- | --- | --- |
| 聊天入口、团队路由、告警分支 | ai-planner ChatService、RouterConfig | ingress、application、runtime/router |
| SOP 逐步执行及变量传递 | ai-planner SOPService | workflows/sop、workflow_versions |
| Skill Hub、资源、版本与同步 | ai-planner Skill/ClaudeSkill 服务 | apps/agent/src/workbench_agent/skill_management、Skill 资产、runtime_profiles |
| MCP 与下游工具 | ai-planner internal/mcp | workbench_agent.tools.mcp、connectors、apps/mcp-server |
| Claude 问答封装、专家和 Hooks | ai-agents adapter、agent-mcp-tools | runtime/experts、policy、tools |
| 仓库和运行目录 | ai-agents repository-manager | workbench_agent.execution、runner |
| 异步请求、事件流和回调 | 两仓库异步任务模块 | runs、run_events、delivery |
| 告警结论、事件和 Jira | ai-planner Event/Jira/DoD 服务 | incidents、actions、connectors |
| Knowledge Gap、成本和反馈 | 两仓库评估与费用模块 | evaluation、usage、feedback |

现有业务规则和连接器可按接口移植或作为 MCP 服务接入。新运行时直接执行模型—工具循环；现有 Smart/Claude 服务可以作为迁移期间的独立引擎，但不充当 LangChain/LangGraph 实现完成的证明。


### 2.3 当前项目实现与验收边界

以下状态截至 2026-11-17，综合当前源码、已有验收记录与本地直接 LLM 联调结果；本地链路可用不代表完整 DoD 业务或生产环境已验收。历史验收记录仅证明其记录日期和范围内的结果。

| 范围 | 已实现或已验证 | 尚待完成或验收 |
| --- | --- | --- |
| Agent 运行底座 | SQLite、Run/Job、持久化、API、Worker、Scheduler、SSE、恢复及打包；本地离线测试覆盖 | 真实部署环境的容量、故障与恢复演练；MySQL/PostgreSQL 后端属于 M5 |
| M2 能力 | 模型配置、Skill、MCP 客户端、工具治理、Agent/Runner 和专家工作流已有实现及本地测试；直接 LLM 问答已有本地真实模型联调 | 真实执行环境、工具及外部业务连接仍需分别验收；模型调用成功不代表知识或诊断链路完成 |
| M3 业务链路 | 本地合成告警、监控/日志查询、SOP、审批、模拟 Jira/SeaTalk 回执及恢复路径已有验证 | 真实 SeaTalk/Jira/DoD、企业数据源、完整根因与备份后动作对账尚未完成验收；受控并行和有界 investigation 尚未完成 |
| M4 工作台 | 工作台提交、任务列表/详情、Agent 管理、模型配置、会话和团队级 Skills 已连接业务 API；支持 Agent 行为配置与发布、v17 单轮及 v18 多轮问答 | 工具连接、定时任务、事件中心、数据统计仍是占位页面；生产登录、细粒度操作体验、SSE/证据/费用等完整 UI 待完善 |
| MCP Server | 项目内独立服务提供知识、指标、日志和 Trace 只读工具；Agent 与服务的本地合成链路已测试 | 企业真实上游接入及隔离验收、容器和 TLS 代理端到端验证尚未完成 |
| 发布和迁移 | 组件已迁入 `workbench/apps/`；已有 Python wheel、Web 构建及临时 SQLite 启动演练记录 | Docker/Compose、生产发布及完整 live 测试集尚未验收；不能以本地单条模型调用替代 |

验证范围、测试数量与限制见 [源码迁移验证记录](../acceptance/2026-11-14-source-migration-result.md)、[M3 本地验收](../acceptance/M3.md)、[MCP 验收](../mcp-server/acceptance.md)；开发与启动以 [本地启动手册](../runbooks/source-layout.md) 为准。

## 3 架构决策与整体架构

### 3.1 选型

| 层次 | 选择 | 责任与理由 |
| --- | --- | --- |
| 语言 | Python 3.12 | 统一异步服务、Agent、数据处理与脚本生态 |
| API | FastAPI、Pydantic v2 | API、认证依赖、Schema、OpenAPI、SSE |
| 管理前端 | Vue 3、TypeScript、Vite、Element Plus、Vue Router | SPA 工作台，构建静态资源由 FastAPI 同源提供 |
| Agent | LangChain 1.x | 模型适配、create_agent、工具循环与结构化输出 |
| 工作流 | LangGraph 1.x | 节点、子图、状态、检查点及人工中断 |
| 首版模型 | DeepSeek + langchain-deepseek | 由 ModelFactory 根据发布的 ModelProfile 构建 ChatDeepSeek，模型名与端点可配置 |
| 数据库 | P0 SQLite 3.51.3+；后续 MySQL 8.4 LTS / PostgreSQL 16+ | 同一 StorageBackend 契约覆盖业务数据、Job、事件、Outbox 和图检查点 |
| 缓存与通知 | Redis 7.2 或更高的受支持版本 | 唤醒、缓存、速率限制；失效后可以降级 |
| 文件 | S3 兼容对象存储 | Skill 包、仓库快照、原始证据、报告和脚本输出 |
| 执行环境 | 容器化 Runner | 文件与脚本执行、依赖镜像、任务隔离 |
| 数据访问 | SQLAlchemy 2 async、Alembic；aiosqlite / asyncmy / psycopg | 驱动和方言位于后端适配器，事务与检查点共用统一存储契约 |
| 可观测性 | 结构化 Logging、OpenTelemetry Tracing、Langfuse SDK 适配（核心链路已实现，真实服务待验收） | 关联请求、Run、模型、工具和用量；暂不接入 Prometheus，设计及实施边界见第 19 节 |
| 依赖管理 | uv、uv.lock | 完整版本锁定；框架升级需经过恢复兼容验收 |

LangChain 的 Agent 是较高层执行抽象，LangGraph 提供有状态流程原语；二者不能代替完整的平台服务。[F1][F2]

### 3.2 架构图

下图是初版目标架构快照；产品名称与 Agent 归属以第 1、5 节为准，当前项目组件及实际部署单元见第 4 节。图中职责边界不表示所有独立进程或真实数据源已经交付。

![DoD Agents Python 整体架构](../assets/workbench-python/01-architecture.png)

[SVG 图](../assets/workbench-python/01-architecture.svg) · [Mermaid 图源](../assets/workbench-python/01-architecture.mmd)

### 3.3 持久化任务选择

run_jobs 表是持久化任务队列，由 JobStore 通过 UnitOfWork 和条件更新领取。P0 使用 SQLite 的短写事务；后续 MySQL/PostgreSQL 可在适配器内用行锁或 SKIP LOCKED 优化竞争，但业务服务不依赖这些 SQL。

API 在同一个数据库事务中写入 Run、输入消息、RunEvent 和 Job，提交成功后才返回 202。Redis 发布唤醒信号以降低轮询延迟；信号丢失或未部署 Redis 时，Worker 仍从 JobStore 轮询领取。

每次进程启动根据配置选择一个 StorageBackend，业务表、Job、执行租约、检查点、事件、预算和 Outbox 必须使用同一后端与数据库。禁止把业务表放入 SQLite、检查点另放 PostgreSQL 后仍宣称具备同一事务内的租约校验。

### 3.4 责任边界

- 控制面管理团队、配置、发布版本、任务与业务事件。
- 执行面读取固定版本的配置，运行图、专家、工具及脚本。
- 工具层检查权限和调用契约，持有下游连接信息。
- 通知和外部业务动作由 Dispatcher 执行，以持久化台账处理重试。
- 图检查点负责恢复执行位置；调度器负责在故障后重新触发执行；运行事件负责向客户端解释过程。


### 3.5 可替换数据库架构

![存储接口与数据库后端](../assets/workbench-python/10-storage-backends.png)

[SVG 图](../assets/workbench-python/10-storage-backends.svg) · [Mermaid 图源](../assets/workbench-python/10-storage-backends.mmd)

存储路径为业务服务 → Repository/Store 接口 → UnitOfWork → StorageBackend。LangGraph 通过 PortableCheckpointSaver 调用同一 CheckpointStore。SQLAlchemy 提供方言能力；事务语义、领取、租约校验和序列化由适配器显式实现，不能只替换连接 URL。[F9][F10][F12][F13]

| 接口 | 统一契约 | 后端责任 |
| --- | --- | --- |
| StorageBackend | create_uow、capabilities、clock、health、migrate | 初始化驱动、连接设置和能力声明 |
| UnitOfWork | begin_read / begin_write、commit / rollback | 短事务边界，禁止跨模型/网络调用持有写事务 |
| RunStore / JobStore | CAS 状态修改、唯一受理、原子领取 | 返回明确成功/冲突，不能以预检查代替约束 |
| ExecutionLeaseStore | 领取、续约、回收、fence guard | 单调 epoch 和受控的执行所有权 |
| CheckpointStore | snapshot、pending writes、history、delete | 与 guard 共用同一事务，字节载荷可迁移 |
| RunEventStore | 单调 seq、提交后回放 | 同事务分配序号，按批读取并及时关闭读事务 |
| ActionStore / OutboxStore | 稳定动作标识、幂等与回执 | 与业务结果在同一事务写入 |
| StorageTransfer | export / import / validate | 版本化逻辑导出，不把 SQL dump 视作跨引擎迁移 |

业务模块只引用接口和领域 DTO；驱动错误转换为 STORAGE_BUSY、STORAGE_UNAVAILABLE、UNIQUE_CONFLICT、STALE_EXECUTION 等统一错误。后端优化不能改变状态、幂等、seq 或恢复语义。

P0 SQLite 部署在同一主机的本地持久磁盘。API、Worker、Scheduler 和 Dispatcher 可以是该主机上的独立进程，统一访问同一个绝对路径；默认一个 Worker 实例、2 个 Run 并发。SQLite WAL 不用于跨主机共享文件系统。[F7][F8]

后续 MySQL/PostgreSQL 适配器通过相同契约验收后，才开放多主机部署与更高并发。SQLite 的 Python 链接库版本由启动检查确认，运行镜像固定 3.51.3+，而不是仅检查 Python 版本。[F7]

## 4 模块组成和部署单元

### 4.1 部署进程

本地部署包含 Agent、Web 前端和可选的独立 MCP Server。Agent 包含以下三个独立的常驻进程；它们分别启动，并使用同一配置和 SQLite 数据库。

| 常驻进程 | 职责 | 部署方式 |
| --- | --- | --- |
| `api` | 提供 HTTP、SSE、Webhook、身份校验、配置和 Run 接口 | SQLite 首版部署在单机；后续服务型数据库通过验收后可多主机多副本 |
| `worker` | 领取 Job、执行工作流、写入检查点和管理预算 | SQLite 首版一个实例、2 个 Run 并发；后续按后端容量扩容 |
| `scheduler` | 扫描定时任务、超时和恢复工作 | 当前与其他进程部署在同一主机；目标支持多副本，通过数据库争抢单项任务 |

以下是相关能力和维护命令，**不是当前需要单独启动的常驻进程**：

| 项目 | 职责 | 当前运行方式 |
| --- | --- | --- |
| Web 前端 | Vue 管理界面，源码位于 `apps/web` | 使用 `npm run build` 生成 `apps/web/dist`；API 从 `/app/` 托管构建产物，无需单独的常驻前端进程。开发时可运行 `npm run dev` 启动独立预览 |
| Dispatcher | 目标是处理 Outbox 通知、Jira/业务动作、重试和结果对账 | 仓库中有相关实现，本地交付验证使用受控模拟器；尚无独立服务启动命令，真实写入尚需验收 |
| Runner | 目标是支持仓库快照、容器内文件和脚本操作及产物上传 | 根据配置由 Worker 使用相关 Runner 能力；尚无独立服务启动命令。`dod runner check` 仅检查运行环境 |
| `db migrate` / `bootstrap` | 数据库迁移和初始配置 | 一次性维护命令，执行完成后退出；首次初始化新数据库时依次执行，已有数据库按维护需求执行 |
| `doctor` | 检查配置和本地存储就绪状态 | 一次性检查命令，执行完成后退出；不启动服务，也不验证外部服务连通性 |

MCP Server 使用自己的 `workbench-mcp-server` 入口独立启动。Scheduler 不执行模型任务。API 不通过 FastAPI BackgroundTasks 承担需要恢复的长期任务。Worker 等待用户时释放执行槽位和活动执行租约。

![当前本地部署进程](../assets/workbench-python/18-deployment-processes.png)

[SVG 矢量图](../assets/workbench-python/18-deployment-processes.svg) · [Mermaid 图源](../assets/workbench-python/18-deployment-processes.mmd)

**本地部署步骤**

1. 在项目根目录设置环境、安装依赖并构建前端。以下使用项目内 `var` 作为本地持久目录；如已有数据库，所有进程应继续使用原来的绝对路径和配置。

   ~~~bash
   export WORKBENCH_ROOT="$PWD"
   export WORKBENCH_ASSET_ROOT="$WORKBENCH_ROOT"
   export WORKBENCH_FRONTEND_DIST_ROOT="$WORKBENCH_ROOT/apps/web/dist"
   export WORKBENCH_PERSISTENT_ROOT="$WORKBENCH_ROOT/var"
   export WORKBENCH_DATABASE_URL="sqlite+aiosqlite:///$WORKBENCH_PERSISTENT_ROOT/dod.sqlite3"
   export WORKBENCH_SKILL_OBJECT_ROOT="$WORKBENCH_PERSISTENT_ROOT/skill-objects"
   mkdir -p "$WORKBENCH_PERSISTENT_ROOT" "$WORKBENCH_SKILL_OBJECT_ROOT"

   cd "$WORKBENCH_ROOT/apps/agent"
   uv sync --frozen --group dev
   cd "$WORKBENCH_ROOT/apps/web"
   npm ci
   npm run build
   ~~~

2. 按环境配置 `config/local/agent.yaml` 和所需凭据。在 `apps/agent` 中，首次初始化新数据库时依次执行迁移和初始化；已有数据库仅在维护时按需迁移。启动前使用 `doctor` 检查配置和本地存储，确认返回 `"ready": true`。

   ~~~bash
   cd "$WORKBENCH_ROOT/apps/agent"
   uv run workbench db migrate --config "$WORKBENCH_ROOT/config/local/agent.yaml"
   uv run workbench bootstrap --config "$WORKBENCH_ROOT/config/local/agent.yaml"
   uv run workbench doctor --config "$WORKBENCH_ROOT/config/local/agent.yaml"
   ~~~

3. 在三个终端分别从 `apps/agent` 启动以下常驻进程。新终端不会继承其他终端临时设置的环境变量；每个终端均需设置与步骤 1 相同的变量及所需凭据，确保指向同一配置和 SQLite 数据库。也可由进程管理器或 Compose 分别管理进程。

   ~~~bash
   # 终端 1：HTTP、SSE 和前端页面
   uv run workbench api --config "$WORKBENCH_ROOT/config/local/agent.yaml" --host 127.0.0.1 --port 8000

   # 终端 2：领取和执行后台任务
   uv run workbench worker --config "$WORKBENCH_ROOT/config/local/agent.yaml"

   # 终端 3：扫描定时任务、超时和恢复工作
   uv run workbench scheduler --config "$WORKBENCH_ROOT/config/local/agent.yaml"
   ~~~

前端构建完成并启动 API 后，可访问 `http://127.0.0.1:8000/app/`。`doctor` 不启动服务，也不验证外部服务连通性。完整配置、MCP Server 与容器部署说明见 [本地启动手册](../runbooks/source-layout.md)。

Skills 管理复用 API，技能加载由 Worker 完成，不新增常驻进程。API 与 Worker 必须使用同一个绝对 `WORKBENCH_SKILL_OBJECT_ROOT`，并保留该目录中的已发布对象。已有 Agent 数据库升级共享 Skills 功能时，先停止服务并备份数据库和对象目录，再执行增量迁移；无需重建 Agent 或清空数据，详见第 21.5 节。

### 4.2 代码组织契约

项目目录为 `/Users/xianguiwang/Projects/workbench/`。三个组件同属一个项目和 Git 仓库，各自保持依赖、构建与测试边界：

~~~text
dod/
  README.md
  docs/                         规格、图源、实施计划、验收与运维手册
  deploy/
    Dockerfile.agent            Agent 与 Web 构建
    Dockerfile.mcp-server       MCP Server 构建
    compose.local.yml           本地编排
  config/
    local/agent.yaml            Agent 本地配置
    local/mcp-server.example.yaml         MCP 本机配置模板
    local/mcp-server.compose.example.yaml MCP 容器配置模板
    workflows/                  工作流 YAML
  apps/
    agent/
      pyproject.toml, uv.lock
      src/workbench_agent/
        settings.py             配置解析
        api/                    HTTP、认证、SSE、前端静态路由
        ingress/                告警、消息规范化
        application/            用例与应用服务
        domain/                 领域对象及规则
        runtime/                Graph、专家、模型、检查点与预算
        skill_management/       Skill 导入、版本与加载
        execution/              仓库、快照、上传与执行资源
        tools/                  MCP 客户端、执行策略和工具适配
        connectors/             连接器
        jobs/                   Job、租约与调度
        incidents/              事件和告警
        delivery/               投递、回执与对账
        evaluation/             反馈与评估
        persistence/            存储接口、SQLite、迁移与检查点
        cli/                    CLI 入口及维护命令
        runner/                 运行目录与受控执行适配
      tests/                    单元、契约、集成、故障和端到端测试
    web/
      package.json, package-lock.json
      src/                      Vue 页面、组件、路由、样式及 SSE 工具
      tests/
    mcp-server/
      pyproject.toml, uv.lock
      src/workbench_mcp_server/       interfaces、application、domain、infrastructure
      tests/
~~~

- Agent 的源码根为 `apps/agent/src`，实际包名为 `workbench_agent`，导入使用 `from workbench_agent.runtime...`、`from workbench_agent.persistence...`。发行包名保持 `workbench-agents`，`dod` 的入口为 `workbench_agent.cli.main:main`。
- MCP Server 的发行包名为 `workbench-mcp-server`，Python 包名为 `workbench_mcp_server`。Agent 的 `workbench_agent.tools.mcp` 作为客户端；服务端发布工具并执行自身的数据范围授权，两者不共享进程。
- `apps/agent/src/workbench_agent/skill_management/` 是管理代码。Skill 包的 `SKILL.md`、references、scripts 和 assets 属于导入或发布资源，不作为 Agent wheel 的 Python 包；Git Skill 导入使用外部受控仓库中的 `skills/<skill-name>/`，本项目不要求存在根目录 `skills/`。
- `config/workflows/` 是项目级工作流来源；数据库、仓库工作副本、凭据与执行产物由部署配置和持久卷管理，不放入源码或镜像构建上下文。
- `WORKBENCH_ASSET_ROOT` 指定绝对资源根，工作流读取相对该根的 `config/workflows/`；`WORKBENCH_FRONTEND_DIST_ROOT` 可单独指定 Vue 产物的绝对路径，缺省回退到 `WORKBENCH_ASSET_ROOT/web/dist`。缺少前端构建时 API 返回 503。
- 运行时不得依赖工作目录猜测资源路径；测试可以通过测试专用路径把项目内 MCP Server 注入解释器，Agent wheel 的生产导入不依赖手工设置 `PYTHONPATH`。
- MySQL/PostgreSQL backend、跨数据库 transfer 和完整 observability 为目标能力，不以目录清单表示已实现。

Agent 当前的打包约定为：

~~~toml
[project.scripts]
dod = "workbench_agent.cli.main:main"

[tool.setuptools]
package-dir = {"" = "src"}

[tool.setuptools.packages.find]
where = ["src"]
namespaces = true
include = ["workbench_agent", "workbench_agent.*"]
~~~

模块通过领域契约和 Repository 交互，运行时不直接操作聊天平台 SDK。连接器接收已经校验的输入和可信调用上下文；安装 wheel 后须在源码目录外验证 CLI、API 和资源加载。

### 4.3 管理模块

管理端以“Agent 管理、模型配置”为当前配置主线，每个产品 Agent 对应 `agents` 中的独立身份，由 RuntimeProfile 管理其草稿和不可变发布版本。同一工作台可配置多个 Agent，不增加独立 Agent 微服务、`agent_type` 枚举或市场。完整 P0 还包括团队/成员、Skill、MCP 连接、仓库、定时任务与事件管理；菜单存在不表示页面已完成，逐页状态见第 20 节。UI 展示业务 API 返回的数据，不直接读取 LangGraph 内部检查点表。

## 5 领域对象与身份

本节定义对象的业务含义、身份来源、生命周期和不变量；第 13 节定义它们的物理存储、字段、索引和事务约束。两者按“领域对象 → 权威记录 → 受控状态转换”对应，不要求一个领域对象必然对应一张表，也不把 JSON 引用视为数据库外键。

当前实现采用领域 DTO、应用服务、Store 与受控事务共同维护这些约束。下文明确标注目标能力和规划对象；字段或表存在，不等于对应业务已经接入。精确物理结构以第 13.2 节当前字典为准，待迁移加固以第 13.10 节为准。

### 5.1 身份与标识

**当前领域契约：**Team 下直接创建多个独立 Agent，每个 Agent 具有 `agents.agent_id` 和自己的 RuntimeProfile；Thread、Run、资源绑定、权限范围与幂等回执直接归属 Agent。管理入口为 `/app/agents`，API 为 `/v1/agents`。不存在 Workspace 产品或领域实体，也不保留 Workspace→Agent 映射层。LangChain 专家 Agent 是图内部执行角色；Runner 的 `/workspace` 是技术运行目录，二者都不等同于产品 Agent。数据库切换与旧契约拒绝边界见第 21.4 节。

#### 认证主体与授权上下文

| 对象 | 内容与可信来源 | 持久化及使用边界 |
| --- | --- | --- |
| VerifiedIdentity | 验证器生成 subject、issuer，可带 team_hint、connector_id；生产用户经 OIDC 验证签名、issuer、audience、期限 | 是认证结果，不是团队角色授权；当前没有独立 users/identity 表，成员关系保存在 team_members |
| AuthContext | 解析后的 team_id、subject、roles、identity_kind、agent_scope | 请求级可信快照；进入用例后仍读取当前有效 Team/Member/Agent，不能把 Token 内角色或调用方 Header 作为最终权限 |
| RunContext | 接受 Run 时冻结的身份、配置与版本引用，详见第 5.2 节 | 保存在 runs.versions；证明受理时使用了哪些身份和版本，不证明这些权限现在仍有效 |
| ExecutionGuard | team_id、thread_id、run_id、job_id、worker_id、run_epoch | 本次执行的所有权凭据；每次受保护写入必须在同事务核对有效 execution_leases 和允许 Run 状态，仅持有对象实例没有写权限 |

AuthContext.identity_kind 目前为 user、connector、development。connector 必须匹配部署注册的服务 subject、团队及 Agent 白名单，且有当前有效成员关系；它不能通过消息正文冒充普通用户或审批人。development 仅用于开发环境，生产拒绝开发身份配置。

team_hint 是来自已验证身份的选队线索，仍须匹配有效成员关系；没有明确团队且同主体匹配多个团队时拒绝歧义选择。agent_scope=NULL 仅表示该身份没有附加的 Agent 白名单，不表示跳过团队、角色、Agent 状态和工具授权。

当前角色按 viewer < operator < maintainer < admin 分级：读取通常要求 viewer，执行要求 operator，配置维护通常要求 maintainer，团队管理按接口要求 admin。最终以用例的明确最低角色及当前成员记录为准；connector 不得执行 maintainer/admin 管理操作。工具权限还要满足用户、团队、Agent、Skill、专家权限交集，角色高不能绕过工具范围与动作确认。

#### 标识的精确语义

| 标识 | 领域含义 | 生成、存储与作用域 |
| --- | --- | --- |
| team_id / agent_id | 租户隔离边界 / 团队内独立 Agent 身份 | 控制面 UUID；分别对应 teams、agents；Run/Thread 以 agent_id 关联具体 Agent，所有关联校验团队与归属 |
| subject | 用户或服务主体 | 身份系统字符串，当前最长 128；不是本地用户 UUID，也不是认证 Token |
| thread_id | 业务会话 | threads.thread_id；承载多条 Message 和多个 Run |
| run_id | 一次业务执行，包括等待和恢复 | runs.run_id；重试、接管和同 Run 的人工恢复均保留，独立新问题创建新 Run |
| job_id | 一个持久调度单元 | run_jobs.job_id 属于某 Run/generation；admin_jobs.job_id 属于管理任务，二者不能混用 Guard |
| message_id | 一条稳定消息身份 | messages.message_id 或 RunEvent 中的草稿身份；草稿不保证已经有 messages 行 |
| HTTP request_id | 单次 HTTP/日志追踪身份 | 服务端或可信网关的追踪概念；当前没有对应独立业务表，不参与业务去重，也不等于恢复请求主键 |
| source_event_id | 本地入口事件记录 | source_events.source_event_id，是本地 UUID，不是 SeaTalk/告警原始事件号 |
| connector_id + external_event_id | 外部事件去重身份 | 与 team_id 组成 source_events 唯一键；外部事件号由经过验证的协议适配器解析 |
| interrupt_id / graph_interrupt_id | 公开等待点 / 框架中断身份 | interrupts 的两个字段；同 Run 的 graph_interrupt_id 唯一，不能只按最新消息猜等待点 |
| resume_request_id | 一次持久恢复输入 | 领域名称，对应 resume_requests.request_id；accepted_resume_request_id 指向它，图内 last_applied_resume_id 记录已应用身份 |
| logical_call_id | 跨恢复稳定的工具逻辑调用 | tool_invocations.logical_call_id；与 Run 和 attempt 一起去重，不能按每次恢复随机生成 |
| invocation_id / invocation | 一次实际尝试的身份 | 工具台账 invocation_id 为 UUID；预算表 invocation 为最长 128 的尝试键，按调用协议关联；实际网络重试使用新尝试身份 |
| action_id / action_key | 外部逻辑动作身份 / 业务去重键 | actions 的 UUID 与团队内唯一业务键；恢复和发送重试保留 action_id，作为下游幂等输入 |
| approval_id / outbox_id | 一次审批 / 持久发送记录 | 同 Action 可保留多次审批历史；当前每逻辑动作一个目标，Outbox 的一对一关系仍主要由服务保证 |
| scope_id / evidence_id / artifact_id | 查询范围修订 / 证据 / 文件对象 | 分别对应 query_scope_revisions、evidence、artifacts；三者不能用可变 URL 或临时目录代替 |
| checkpoint_id / namespace | 框架检查点 / 主图或专家子空间 | 不等于业务 Thread 或 Job；与 run_id、namespace_hash 共同定位状态和父链 |

业务 thread_id 与 LangGraph 的 configurable.thread_id SHALL 区分：图执行固定使用 run_id。恢复沿用同一 run_id 和已发布 Graph/Codec；不同 Run 不共享检查点、额度或当前证据范围。专家子任务在同 Run 下使用稳定 checkpoint namespace，不复制成新的独立预算 Run。

同一 Thread 同时最多一个非排队活动 Run，以 threads.active_run_id 为逻辑位置；waiting_input/waiting_approval 仍保留位置，但释放执行租约和 Worker 槽位。通用入口的新问题可排队，补充信息必须投向指定 Run/interrupt；显式会话请求还会检查未终结 Run，冲突返回 `THREAD_BUSY`。巡检每次默认创建独立 Thread/Run。v17 只消费当前问题，v18 显式加载同 Agent、同 Thread 的授权历史完整问答，见第 7.5.4 节；没有自动摘要或跨会话长期记忆。

### 5.2 不可变 RunContext 与可修订 QueryScope

RunContext 是执行依据的接受快照，由 ContextResolver 与接受事务构造；用户正文、模型输出和工具返回不能提供或替换它。当前字段及其权威来源如下，JSON 结构与第 13.8 节保持一致。

| 字段 | 类型 | 来源与不变量 |
| --- | --- | --- |
| team_id、agent_id | string UUID | 从认证与授权后的 Agent 解析，接受后不改团队/Agent |
| subject、identity_kind | string | 发起者与身份类别快照；不保存 Token |
| agent_scope | string[] 或 NULL | 接受时的身份 Agent 附加限制；NULL 的含义见第 5.1 节 |
| roles | Role[] | 接受时数据库成员角色快照，仅用于复现和审计；不能替代当前授权 |
| accepted_at_ms | int | 同一接受事务的 StorageClock UTC 毫秒时间 |
| source | string | 可信接受服务设置的来源键，默认 api；协议来源另有 source_events/来源快照证明 |
| agent_profile | VersionRef | 精确 runtime_profiles.profile_id、version、hash |
| graph_version | string | 固定已注册 Graph 实现；恢复不能换最新版 |
| default_model | VersionRef | 指向 model_profile_versions.model_version_id 的发布引用 |
| role_models | {role,ref}[] | router/knowledge/diagnosis/code/summary/evaluation 的可选角色绑定，无绑定时回退 default_model |
| system_prompt | string，默认空 | Agent 已发布配置中的系统提示词快照，最多 12000 字符；目前由 v17/v18 消费，不能授予额外权限 |
| response_language、response_format | auto/zh/en；default/markdown/json | 回复偏好快照；属于模型提示指令，不构成严格输出校验 |
| conversation_history_turns | int，0–8，默认 8 | v18 最大历史完整问答轮数；0 表示不传历史，实际还受上下文和预算限制 |
| skills | VersionRef[] | 允许加载的已发布 Skill 版本集合 |
| tools | VersionRef[] | 允许使用的已发布工具绑定集合；仍需当前权限及 Scope 校验 |
| repositories | VersionRef[] | 固定 repo_snapshots.snapshot_id、完整 commit 和归档摘要 |
| workflow | VersionRef 或不出现 | SOP/inspection 接受时固定的 workflow_versions 版本 |

VersionRef 是 `{resource_id,version,content_hash}` 值对象，不是某个通用 versions 表的外键。模型、Skill、工具和仓库引用分别解析到其所属表；发布和受理同时核对 team/Agent/ID/version/hash。表内版本序号与 JSON 字符串版本严格转换，不能按字符串比较大小。

**冻结配置、实时授权和业务范围分开处理。** RunContext 保存身份和已选版本；AuthContext/当前成员关系回答“此刻是否允许”；ExecutionGuard 回答“本次尝试是否仍拥有执行权”；QueryScopeRevision 回答“本次证据查询允许分析哪一段业务范围”。预算上限位于 runs.budgets，真实消耗位于 usage_*，当前 Scope 不嵌入并覆盖 RunContext。

QueryScopeRevision 对应 query_scope_revisions：scope_id、run_id、version、scope_json、hash、created_by 和时间戳。当前 scope_json 允许 region、service、environment、start_at、end_at；全部为字符串，时间窗使用带时区 RFC 3339。缺区域的初始修订可以先持久化，但完整取证前必须经业务解析器校验；存下 JSON 不表示范围已完整。

需要补充或修订范围时，可信节点检查指定 interrupt/预期 Scope/当前授权，再由 ContextResolver 创建新修订；同 Run 当前 Scope 按最大修订版本解析。不得原地改写旧范围或让模型扩大服务、环境、区域白名单。时间锚点确定后各来源沿用同一时间窗；本地已实现的区域补充保持原窗口，其他显式窗口修改也必须新增 Scope 并经过相应业务入口验证，不能宣称当前任意修订 API 已具备。

Scope 改变后，旧证据作为历史保留，不能用于当前范围的正常结论、审批或回执复用。当前 evidence.scope_id 与其 run_id 的同属校验仍部分由应用承担，待新增复合 FK 见第 13.10 节。

排队/等待期间发布新配置不影响原 Run 的固定引用；权限撤销仍即时生效。缺版本、Codec 或对象时进入明确不可用/阻塞状态，不静默换模型或 Skill。secret_ref 只保存引用，真实凭据在当前授权的调用作用域解析，不进入 RunContext、GraphState、消息、事件或产物。

### 5.3 GraphState 与持久事实边界

GraphState 是图恢复所需的执行状态，物理存储为 graph_checkpoints 和 graph_pending_writes；它不替代 runs、query_scope_revisions、evidence、usage_* 或 actions 的权威记录。图的 schema_version 与 graph_version 是各自版本契约，当前由注册表/Codec 配对，不保证每个历史 GraphState 都实际含同名字段。

当前不同发布图使用独立 TypedDict：基础图、RCA 图和 SOP 图的字段不同。下表定义逻辑职责与合并约束，不表示已有统一大 GraphState，也不授权给旧版本检查点补写字段。

| 逻辑状态 | 表示与更新规则 | 权威边界 |
| --- | --- | --- |
| 执行身份 | run_id 固定；可信执行上下文由 runtime.context 注入 | context_ref 是目标引用表达，不是当前所有图都有的物理字段；授权和 Guard 不从模型消息还原 |
| messages | 图内消息按稳定 message_id 更新；子图只保留自身循环需要的消息 | 业务会话消息另在 messages，SSE 草稿另在 run_events；三者不能直接当作彼此完整替代 |
| 当前范围 | RCA/SOP 当前使用 scope_ref，部分初始步骤使用 initial_scope_id | 领域 query_scope_ref 对应 query_scope_revisions.scope_id；只有可信上下文节点可变更，不接受专家直接覆盖 |
| 路由与 Skill 选择 | mode/route 与有限版本引用；只能从已发布集合选择 | selected_skill_refs 等是目标逻辑状态；当前部分 select_skills 节点仍仅阶段占位 |
| 证据视图 | 当前图多保存 evidence_refs/baseline_refs 与来源状态；需要字典时按 evidence_id 合并 | 正文和质量以 Evidence/当前 Scope 为准；同 ID 不同内容应报冲突，不静默覆盖 |
| 假设 | 目标按稳定 hypothesis_id 合并支持/反对证据，保留冲突 | 当前有合并辅助函数，不代表完整跨专家假设调查已接入；不能只凭置信度判完成 |
| 步骤结果 | SOP 当前保存 order/index/results/stopped 等；results 按 step_id 分开 | 是检查点内容，当前无独立 step_runs 表；步骤输出不能修改身份/版本/范围 |
| 缺参与后续步骤 | 由校验节点替换明确结构，不追加无限文本 | missing_fields/next_steps 是目标逻辑表达；以实际发布图 Schema 为准 |
| 恢复应用标记 | last_applied_resume_id 在输入节点完成时写入同步检查点 | 对应 resume_requests.request_id，是避免重复 Command 的事实依据 |
| outcome | 候选 RunOutcome 先保存检查点，经验证后由最终投影事务落库 | 图内候选不是已提交结果，也不是外部动作已发送证明 |

父图只合并专家结构化产出与证据引用，子图使用稳定 namespace 保留自身恢复历史；专家、重试和评估共用父预算。图状态默认上限 2 MiB，大原文和文件使用 Artifact/有界证据引用。不得持久化客户端、数据库连接、实时凭据或可由用户指定的可执行反序列化对象。

### 5.4 Evidence 与 RunOutcome

#### 证据对象与来源证明

Evidence 表达“在某个已授权 Scope、通过某次可追踪读取获得的观察”，不是一段孤立自然语言。当前以 evidence 元数据、evidence_payloads 有界正文或 Artifact、QueryScope 及工具回执共同组成完整证据对象。

| 领域属性 | 当前权威记录 | 校验要求 |
| --- | --- | --- |
| 身份与归属 | evidence.evidence_id/team_id/run_id/scope_id | 同团队、同 Run、指定 Scope；仅知道 UUID 不等于可读取 |
| 来源 | evidence.source、tool_invocations 的绑定/快照回执、RunContext 固定版本 | 当前没有独立 source_type/tool_binding_id/invocation_id 证据列；完整来源由回执关联与内容核对，不虚构物理外键 |
| 查询范围与时间 | QueryScope.scope_json、evidence.time_window、来源载荷 | 监控/日志/Trace 核对同一窗口；仓库证据固定 commit/path/行号，值班查询可记录 observed_at_ms；observed_at 不都是独立列 |
| 内容与可访问性 | evidence.summary、evidence_payloads.payload/truncated、evidence.artifact_ref | summary 可能只是 digest；引用有效需正文/对象真实可读且授权仍有效，不能只检查摘要非空 |
| 质量 | evidence.quality | 领域 quality_status 映射为 valid/empty/unavailable/conflicting/stale，当前还使用 truncated；截断必须显式显示 |

valid 表示取证与范围校验通过，不单独证明根因或业务健康；empty 表示真实查询未命中，unavailable 表示工具/来源不可用。conflicting、stale、truncated 不能被汇总节点改成“正常”。服务失败不能归为“知识不存在”；当前范围或读取权限撤销后，旧正文和结论需要重新裁剪披露。

#### 最终分析结果

RunOutcome 是值对象，经 Schema、证据、Scope、必要步骤和预算规则校验后，以 run_outcomes 保存一份最终结果，并关联最终 assistant Message。其字段和嵌套 JSON 详见第 13.8 节；主要语义如下。

| 字段 | 领域含义与约束 |
| --- | --- |
| schema_version / kind | 当前版本字符串 1；foundation.recorded.v1 仅表示底座记录，不能声称业务诊断；analysis.v1 表示分析结果 |
| completed / summary | 非空摘要；completed=false 必须列出 unresolved_items，true 不得还有未完成项；模型不能靠填 true 绕过检查 |
| findings | 已观察事实/正常/不确定发现；每项带证据，normal 需要当前有效 valid 证据及必需检查完成 |
| root_cause_hypotheses | 带证据的原因假设，状态 unverified/supported/rejected；观察和根因分开 |
| impact | 可为空；存在时给出影响和支持证据 |
| evidence_refs | 本结果引用集合；子项引用必须包含于此，且属于当前 Run/Scope |
| unresolved_items | 缺证据、冲突、范围、权限、预算或未完成步骤的明确说明 |
| proposed_actions | 分析建议；不等于 Action 已建立、已批准或已发送 |
| knowledge_gaps | 知识问题候选说明；不等于已审核的 knowledge_gaps 表记录或已发布知识 |
| usage_summary | 本结果的用量视图；未知 Token/费用保留 NULL/pending，实际权威来自 usage_* |

最终事务以稳定 result_id、message_id 和 digest 防止重复落库；物理 run_outcomes.run_id 主键保证每 Run 一份结果。候选完成到公开终态的转换见第 13.9 节：有效 completed=true 对应 succeeded，未完成但有效结果对应 partial；取消/硬超时/失败不能靠晚返回候选覆盖。读取曾保存的 response 快照时仍重新检查当前权限。

### 5.5 领域对象、关系与存储映射

本节以一张总览和六张分组 ER/关系图说明对象归属、基数和权威引用，随后保留存储映射速查表。每图仅展开关键字段，完整字段、复合主外键、索引和事务约束见第 13 节。

**读图约定**

- 灰色实线用于领域总览；蓝色实线表示当前 Schema 已声明对应 FK；紫色虚线表示 JSON、回执或服务层关联。连线只展示本图主题，不穷举全部外键。

- 常规关系箭头按父对象→子对象阅读；选中指针和 JSON 引用按持有方→被引用对象阅读。`1 : 0..N` 表示子方可为零到多条、每条对应一个父方；`0..1 : 0..N` 表示子方的父引用可以为空。

- 字段前 PK/FK 是关键字段提示，部分复合键省略了 team_id 等列；不能据图推断完整键或级联删除。实际 FK 与业务期望的一对一不同之处，在图注中明确说明。

- 规划表和待新增约束没有画成当前物理结构；当前尚无真实业务闭环的部分仍以对应验收记录为准。

[查看完整关系图集](../assets/workbench-python/domain-relations.html)。图集为离线 HTML，可横向滚动；各图同时提供 PNG 阅读版、SVG 矢量版和 Mermaid 图源。以下图集保留重构前快照，其中 Workspace 标签不再表示当前领域实体；当前直接归属关系为 Team → Agent → Thread/Run，精确字段与约束以第 13.2 节为准。


#### 领域关系总览

从 Thread 与 Run 开始阅读：一个会话承载多次执行；Run 产生范围修订、证据、最终结果与外部动作。总览表达领域归属，后续分图再区分物理 FK 与逻辑引用。

![领域关系总览](../assets/workbench-python/11-domain-overview.png)

[SVG 矢量图](../assets/workbench-python/11-domain-overview.svg) · [Mermaid 图源](../assets/workbench-python/11-domain-overview.mmd)


#### 配置与版本固定

Agent 发布 RuntimeProfile；Run 固定 Profile 和模型/Skill/工具/仓库的精确版本。为突出冻结关系，图中省略 Skill 管理身份、成员及部分团队外键；这些对象仍见下方映射表。虚线引用需要逐次核对 ID/version/hash 和当前授权。

![配置与版本固定](../assets/workbench-python/12-config-version-er.png)

[SVG 矢量图](../assets/workbench-python/12-config-version-er.svg) · [Mermaid 图源](../assets/workbench-python/12-config-version-er.mmd)


#### 执行、等待与恢复

Thread 的逻辑活动位置、执行租约与 Run Job 分开；等待点可保留多次恢复提交，但 accepted 指针只选择一条有效请求。图中 accepted 线按持有方指向选中请求；检查点与 pending writes 保留完整恢复语义。

![执行、等待与恢复](../assets/workbench-python/13-run-recovery-er.png)

[SVG 矢量图](../assets/workbench-python/13-run-recovery-er.svg) · [Mermaid 图源](../assets/workbench-python/13-run-recovery-er.mmd)


#### 范围、证据与最终结果

同一 QueryScope 下可收集多份证据，正文与大文件分别保存。工具到证据通过回执关联，Outcome 通过 evidence_refs 引用同 Run 的证据；两者都不是独立物理 FK。现有 FK 不自动保证 Scope 与证据属于同一 Run，应用仍须校验。

![范围、证据与最终结果](../assets/workbench-python/14-evidence-outcome-er.png)

[SVG 矢量图](../assets/workbench-python/14-evidence-outcome-er.svg) · [Mermaid 图源](../assets/workbench-python/14-evidence-outcome-er.mmd)


#### 调用尝试、预算与结算

逻辑工具调用、实际尝试、额度预留、可信结算和迟到回执各自有身份。usage_records.reservation_id 当前只以 FK 保证同团队存在，数据库层并未限制一个预留只能关联一行；服务必须检查同 Run/invocation 和单次结算，待加固见第 13.10 节。

![调用尝试、预算与结算](../assets/workbench-python/15-tool-usage-er.png)

[SVG 矢量图](../assets/workbench-python/15-tool-usage-er.svg) · [Mermaid 图源](../assets/workbench-python/15-tool-usage-er.mmd)


#### 审批、外部投递与事件关联

审批与发送分别留痕，事件外部关联可追溯到已确认 Action。图中 Action→Outbox 标注当前数据库允许的 1:0..N；本期业务期望一个动作一个发送目标，不能把待迁移的单例唯一约束画成已实现。分析终态不等于发送成功，unknown 不能自动重发。

![审批、外部投递与事件关联](../assets/workbench-python/16-action-delivery-er.png)

[SVG 矢量图](../assets/workbench-python/16-action-delivery-er.svg) · [Mermaid 图源](../assets/workbench-python/16-action-delivery-er.mmd)


#### 巡检、工作流与独立 Run

Schedule 固定工作流版本，定时与手动触发分别留历史。定时 Fire 的 run_id 可空，代表未产生执行；数据库层一个 Run 可被多条定时 Fire 引用，服务实际为每次已受理触发创建独立 Thread/Run。手动触发另有同团队 Run 唯一约束。

![巡检、工作流与独立 Run](../assets/workbench-python/17-schedule-run-er.png)

[SVG 矢量图](../assets/workbench-python/17-schedule-run-er.svg) · [Mermaid 图源](../assets/workbench-python/17-schedule-run-er.mmd)


#### 对象与存储映射速查

下表按业务边界组织对象，不把表逐一包装成独立服务；具体字段、PK/UK/FK 及暂未落实的数据库约束仍以第 13 节为准。

| 领域对象与关系 | 当前权威表/字段 | 生命周期与边界 |
| --- | --- | --- |
| Team 1:N Member、1:N Agent | teams、team_members、agents | 当前成员是身份授权基础；删除采用限制/状态撤销，不能破坏在途 Run 审计 |
| Agent 1:N RuntimeProfile，0:1 当前指针 | runtime_profiles、agents.active_profile_id | 草稿可编辑，发布新增不可变版本，切指针仅影响新受理 Run |
| ModelProfile 1:N ModelVersion | model_profiles、model_profile_versions | 模型身份、草稿和运行版本分开；active_version 不替代 Run 已固定引用 |
| Skill 1:N SkillVersion；Tool 草稿与发布版 | skills/skill_versions、tool_binding_drafts/tool_bindings | 版本内容不可变，当前授权可撤销；JSON 引用需服务验证 |
| Repository 快照、WorkflowVersion | repo_snapshots、workflow_versions | 固定 commit/内容摘要或 DAG 版本；当前没有独立 repositories/workflows 身份表，不假定这些表已存在 |
| Thread 1:N Message、1:N Run，0:1 活动 Run | threads、messages、runs | 逻辑活动位置与执行租约分离；消息/Run 同属由事务检查 |
| Run 1:N Job，Thread 0:1 ExecutionLease | run_jobs、execution_leases | generation 管续执行 Job；epoch 管执行所有权；waiting 释放租约但保留 Run |
| Run 1:N Interrupt；Interrupt 1:N ResumeRequest，0:1 有效接受指针 | interrupts、resume_requests | 请求历史可保留，唯一有效请求由 accepted 指针 CAS 选定 |
| Run 1:N QueryScopeRevision/Evidence/RunEvent，1:0..1 Outcome | query_scope_revisions、evidence、run_events、run_outcomes | 范围、观察、过程和最终结论分开，不能互相覆盖冒充 |
| Run 1:N 工具尝试/额度预留/实际结算 | tool_invocations、usage_reservations、usage_records | 稳定逻辑调用与实际尝试分别去重；迟到计量走 usage_quarantine |
| Run 1:N Action；Action 1:N Approval，当前一个发送目标 | actions、approvals、outbox | 参数绑定审批、发送台账、未知结果查证独立于分析完成 |
| Run 1:N Artifact；证据可引用 Artifact | artifacts、artifact_upload_scopes、evidence.artifact_ref | 对象完成并校验后才 available；对象与数据库非同事务 |
| Incident 1:N 外部关联；关联可追溯 Action | incidents、incident_links、incident_action_links | 当前自动业务投影仅合成告警及本地模拟回执，真实业务仍待接入 |
| Schedule 1:N 定时/手动 Fire；已受理 Fire 关联 Run | schedules、schedule_fires、schedule_manual_fires | due_at 去重、时区/重叠/错过策略独立于 Run 执行结果 |
| 管理任务、反馈、知识候选、审计 | admin_jobs/admin_job_events、feedback、knowledge_gaps、audit_logs | 各自授权和状态；管理任务不是模型 Run，反馈/评估失败不改已提交主结果 |

团队级/Agent 级幂等回执分别在 team_idempotency_records/idempotency_records，来源事件去重在 source_events；它们记录已提交身份和响应，不替代业务对象。synthetic_* 表与部署策略单例保存测试来源/动作授权证明，不是生产业务实体。旧系统 import_records/migration_routes 是第 13.11 节规划对象，当前尚无物理表。

### 5.6 版本、并发代数与恢复身份

| 维度 | 改变时机 | 不能替代的概念 |
| --- | --- | --- |
| 资源 version + content_hash | 发布新的模型/配置/Skill/工具/快照版本 | 不表示写锁或当前授权；原 Run 保留原引用 |
| Agent/Run/策略 revision | 对应对象 CAS 变更 | 不表示执行所有权，不应当作发布版本 |
| Job generation | 同 Run 创建新的续执行/恢复调度单元 | 不等于真实网络重试次数 |
| Run run_epoch | 原子获得/接管执行所有权 | 旧 epoch 的检查点、事件和业务写入必须被拒绝 |
| Job attempt / invocation attempt | 实际执行或调用尝试 | 纯投影修复/幂等重放不应消耗新尝试；具体计数按领取/调用台账解释 |
| QueryScope version | 显式创建新的业务范围修订 | 不刷新预算，不替换 Run 固定配置 |
| RunEvent seq | 同事务提交一条过程/结果事件 | 不等于 Message 次数、执行 attempt 或图 checkpoint_id |
| guard_revision / outbox.revision | 管理/租约或发送任务的内部并发控制 | 不可拿 Run 的 Guard 管理外部发送或独立资源清理 |

公开 Run 状态、run_jobs 调度状态、Action 状态、Outbox 投递状态是四套协议。Run 的 queued 不等于 Job 的 ready/retry_wait/capability_blocked；分析 succeeded/partial 不代表 Outbox 已确认；Approval 的 approve 不代表下游请求已经发送。完整状态转换见第 14 节，原子写入组合见第 13.9 节。

### 5.7 外部动作、预算和对象可用性

模型提出建议后，只有可信业务服务能创建持久 Action。Action 固定业务 action_key、工具 VersionRef、QueryScope、目标和规范化参数摘要；Approval 绑定这些值、策略哈希、允许审批的真实主体和过期时间。当前允许确认的行为通过明确接口完成，不由模型文字“批准”触发。

批准后产生/授权 Outbox，Dispatcher 先提交 sending 标记再 I/O，回来后按发送租约提交回执。Action ID 在恢复/重试中稳定；丢响应是 unknown，不得换 action_id 假装新动作绕过去重。查证使用部署所有的可信 lookup；IncidentLink 只有在回执已确认并可追溯 Action 时才表示“已创建/已通知”。当前真实 sender 和对应闭环仍需接入，本地模拟回执不能当真实交付。

BudgetReservation 表示一次实际调用的额度占用，UsageRecord 表示可信计量，UsageQuarantine 保存失效执行晚返回的计量事实；它们不能通过 GraphState 中的计数覆盖。专家、重试、汇总和评估共用父 Run 预算；summary_tokens 接受时固定。待确认用量继续保守占额，未知费用保持 pending，不按零结算或在恢复时重新获得额度。

Artifact 表示经过授权的对象元数据，available 必须有对象存在、大小/摘要校验及当前范围证明；上传暂存、Runner stdout 摘要、对象键和预签 URL 是不同概念。Run/租约结束不一定意味着资源已清理，runner_cleanup_obligations 使用独立维护身份记录并重试清理，不能复用失效 Run Guard 写结果。

### 5.8 实现与设计边界

- 当前身份验证、授权、冻结 RunContext、版本引用、Scope 修订、运行/恢复台账和底座检查点已有实现；具体部署可用性须另验。Team/Agent 归属、证据/Scope 同 Run 等约束部分依赖服务检查，第 13.10 节待新增 FK/唯一约束不能提前算作数据库保证。
- GraphState 表是逻辑职责说明；当前各版本图字段不同，Skill 选择、多轮历史和完整假设调查仍有接线缺口。新增统一结构或行为需新 Graph/State 版本，不能就地改写可恢复旧 Run。
- Evidence/Outcome 的契约包含完整业务目标；当前 RCA/SOP 等路径仍可能固定输出 partial，合成 Incident 不证明已完成真实根因诊断。
- 第 5 节负责解释对象语义，第 13.2/13.8 节负责字段与载荷，第 13.9 节负责事务不变量，第 14–17 节负责状态、恢复和授权流程；实现变化须同步这些章节与验收记录。

## 6 启动与关闭

### 6.1 启动依赖

![启动时序](../assets/workbench-python/02-startup.png)

[SVG 图](../assets/workbench-python/02-startup.svg) · [Mermaid 图源](../assets/workbench-python/02-startup.mmd)

1. 平台准备 SQLite 本地持久目录、身份配置、所需运行镜像和对象存储；Redis 可选。后续服务型数据库后端另行准备数据库服务。
2. migrate 在停止写入的维护窗口，以 SQLite 事务锁/后端迁移协调机制执行业务和统一检查点表的 Alembic 迁移。普通服务不执行 DDL。
3. bootstrap 校验配置，创建团队、角色、Agent、发布的 RuntimeProfile 及基础 Skill/工具绑定。
4. API 的 FastAPI lifespan 加载配置、连接池、认证验证器和版本注册表，核对 Schema 后开始受理。[F6]
5. Scheduler 启动到期任务扫描、租约扫描与投影协调；Dispatcher 启动 Outbox 扫描。
6. Worker 注册所支持 Graph 版本、建立调用客户端池与执行容量，开始领取可执行 Job。
7. Runner 注册镜像与执行能力。MCP 工具发现结果记录为连接健康状态；发现成功与业务调用成功分别标记。

### 6.2 健康与就绪

| 检查 | 条件 | 失败行为 |
| --- | --- | --- |
| live | 主事件循环存活 | 由部署平台重启 |
| API ready | 当前 StorageBackend 可用、Schema 匹配、认证配置有效 | 不受理新请求，返回 503 |
| Worker ready | 后端可用、Graph/Codec 可加载、存在可用执行槽位 | 停止领取新 Job |
| Redis | 通知/缓存可用 | 使用数据库轮询与数据库额度检查，标记 degraded |
| 对象存储 | 可读取基础 Skill/快照 | 对依赖该存储的 Profile 停止派发 |
| 模型/MCP | 配置有效、最近调用健康 | Profile/工具级降级，不令全部 API 失效 |
| Runner | 心跳、镜像和容量有效 | 代码/脚本步骤等待或失败，纯知识任务可继续 |

启动探测不自动产生模型付费调用或执行有副作用的工具。连通性检查与真实业务探针分开，并标明配置、发现、调用三个验证层次。

### 6.3 优雅关闭

API 停止受理后给现有请求最多 15 秒完成；SSE 连接发送可重连提示后关闭。Worker 先退出 ready，再停止领取，给运行节点最多 30 秒结束并保存已完成边界；超期则取消本次尝试并释放数据库资源，留下可恢复 Job。Dispatcher 只在结果台账提交后确认 Job。Runner 终止任务子进程组并记录已上传产物。

关闭和断连均不把未完成 Run 标记为 succeeded。等待用户的 Run 已保存检查点，不占用 Worker，因此无需为它维持进程。

## 7 Agent 与工作流设计

本章按当前源码说明执行行为，包含 `dod.v17` 单轮和 `dod.v18` 多轮直接问答。图注册表示 Worker 能识别该版本；Agent 能否使用它，还取决于已发布 Profile、请求模式、模型凭据，以及该图需要的工具、数据源或仓库授权。版本号表示工作流执行契约，不是 Git 分支，也不是功能逐版完全包含的产品版本。

### 7.1 工作流选择、配置与版本固定

一次请求涉及以下独立配置：

| 配置 | 作用 | 运行时来源 |
| --- | --- | --- |
| `graph_version` | 选择 Python 中构建的 LangGraph 图和执行器 | Agent 已发布 Profile；保存进 RunContext |
| Agent 行为与上下文策略 | 系统提示词、回复语言/格式、历史轮数与预算限制 | Agent 草稿显式发布；受理时冻结行为配置和最终预算，不在执行中读取最新草稿 |
| 图状态 Schema 版本 | 约束该图的检查点状态 | GraphRegistry 中与图版本关联的 Schema；v2/v3 共用 `dod-state-v2` |
| `WorkflowVersion` | 定义 SOP/巡检的步骤、依赖、工具参数和条件 | 请求指定的已发布工作流；固定 ID、版本和内容哈希 |
| 模型、工具、Skill、仓库版本 | 定义执行图时可使用的资源 | RunContext 中固定的版本引用；使用时仍检查当前权限 |

API 在 `RunService.create` 中读取 Agent 已发布 Profile，先校验图与模式的组合，再解析并固定 RunContext、预算与工作流引用。普通 Run 请求通过 `mode` 表达用途；图由 Profile 决定。保存草稿不改变线上行为；发布后只有新 Run 使用新配置，已有 Run 和恢复任务继续使用原快照。描述是配置元数据，不自动作为系统提示词注入。

当前入口的主要限制如下：

| 场景 | 受理规则 |
| --- | --- |
| 知识 QA：v16 | 仅 `mode=qa`；`business_context.query_scope` 必须通过范围校验，否则返回 `INVALID_SCOPE`（422） |
| 直接 QA：v17 | 仅 `mode=qa`；`business_context` 中出现 `query_scope` 键即返回 `WORKFLOW_UNAVAILABLE`（409），即使值为空也不接受 |
| 多轮直接 QA：v18 | 仅 `mode=qa`，不接受 `query_scope`；显式 `business_context.conversation=true` 要求 v18，已有会话还须通过并发及历史兼容检查 |
| SOP/巡检：v11/v12 | 仅 `sop`、`inspection`，且必须提供有效 `workflow_version_id`；其他图不能承接这两种模式 |
| 合成告警：v13–v15 | 需要受信合成告警接入能力、调用方事务、connector 身份、来源连接器及 `mode=rca` |
| 所有请求 | 模式必须在 Profile 的 `allowed_modes` 中；凭据、版本与预算还需通过独立校验 |

注册表见 [registry.py](../../apps/agent/src/workbench_agent/runtime/graphs/registry.py)，入口校验见 [runs.py](../../apps/agent/src/workbench_agent/application/runs.py)。

### 7.2 已注册的工作流与实际拓扑

| 版本 | 构建模块 | 实际节点链及用途 |
| --- | --- | --- |
| `dod.v1` | `foundation.py` | `prepare → input → compose`；基础输入记录 |
| `dod.v2`、`dod.v3` | `interrupts.py` | `prepare → input → compose`；input 可中断等待输入或确认；v3 增加公开请求字段映射，复用 v2 的图和状态 |
| `dod.v4` | `dod.py` | 通用八节点链；investigate 尝试使用 Diagnosis 专家，分支接受 rca/inspection；当前公共入口另限制 inspection 只能走 v11/v12 |
| `dod.v5` | `qa.py` | 通用八节点链；investigate 使用 Knowledge 专家，接受 qa |
| `dod.v6` | `code.py` | 通用八节点链；investigate 使用 Code 专家，接受 code 且要求一个固定仓库快照 |
| `dod.v7` | `multi.py` | 通用八节点链；在 code 模式下依次调用 Knowledge、Code 并合并引用 |
| `dod.v8` | `extractive.py` | 延续 v7 的专家调用，compose_outcome 增加来源原文摘录选择 |
| `dod.v9` | `rca.py` | `prepare → input → scope → compose`；告警范围解析和人工补充 |
| `dod.v10` | `rca_baseline.py` | `prepare → input → scope → bind_baseline → collect_baseline → compose`；范围绑定后的指标、日志、Trace 基线采集 |
| `dod.v11`、`dod.v12` | `sop_graph.py` | `prepare → step（按索引重复）→ compose`；v12 支持条件跳过和输出 Schema 校验 |
| `dod.v13` | `rca_synthetic.py` | 与基线 RCA 相同的节点链，用于受控合成告警 |
| `dod.v14`、`dod.v15` | `rca_proposal.py`、`rca_batch.py` | 在 collect_baseline 后增加 propose；分别调用单项、批量合成动作提案接口 |
| `dod.v16` | `qa_v16.py`、`qa_synthesis.py` | 通用八节点链；范围限定的知识检索、证据校验和抽取式回答 |
| `dod.v17` | `direct_qa.py` | `START → answer → END`；直接调用模型，无专家工具循环 |
| `dod.v18` | `conversation_qa.py` | `START → answer → END`；授权有界历史 + 当前问题，直接调用模型，不调用工具 |

上表模块均位于 `apps/agent/src/workbench_agent/runtime/graphs/`。bootstrap 默认 Profile 仍为 v3，只记录输入，不调用模型；注册新图不会自动切换已有 Agent。普通单轮问答选择 v17，连续对话选择 v18；v16 用于授权知识问答，不能把它视作直接问答的必经升级步骤。

#### 7.2.1 通用八节点链具体做什么

v4–v8 和 v16 都构建下面的顺序链，并通过 `compile(checkpointer=saver)` 接入检查点：

~~~text
START → prepare_context → route → select_skills → collect_baseline
      → investigate → validate_evidence → compose_outcome → finalize → END
~~~

| 节点 | 当前代码行为 | 状态影响 |
| --- | --- | --- |
| `prepare_context` | 调用 `check_operation`，要求图的 thread_id 等于 Run ID，构造稳定 ID 的 HumanMessage | 更新 phase 和 messages |
| `route` | 直接复制请求中的 mode；未启用模型分类器 | 写入 route；没有通过条件边切换到另一张图 |
| `select_skills` | 检查执行有效性并记录阶段；本节点尚未实现动态 Skill 检索和选择 | 更新 phase |
| `collect_baseline` | 检查执行有效性并记录阶段；通用链中没有在这里采集监控基线 | 更新 phase；真正的 RCA 基线逻辑在 v10/v13–v15 |
| `investigate` | 按版本选择专家，收集证据引用；能力缺失、范围变化或预算不足时记录未完成原因 | evidence_refs、unresolved、diagnosis_done |
| `validate_evidence` | 用 EvidenceValidator 检查范围、证据质量、引用可访问性和必需调查步骤 | validation，含 valid_refs、invalid_refs、missing_steps 等 |
| `compose_outcome` | v4–v7 组织保守的部分结果；v8/v16 可调用模型选择原文摘录 | outcome |
| `finalize` | 当前这些图中仅检查执行有效性并更新 phase | 业务结果由外层 ResultProjector 和 ResultService 提交 |

`DoDState` 使用 `TypedDict` 描述状态，`messages` 使用 `add_messages` 合并；节点返回增量字段。权限、数据库、租约等运行能力通过 `Runtime.context` 的 Execution 提供。`route` 是状态字段，`phase` 是阶段标记，两者都不等同于 Run 的 queued/running/partial 等业务状态。

#### 7.2.2 通用目标主图

下图保留为目标架构参考。其中自然语言分类、动态 Skill 选择、统一 await_input/await_approval 分支及更广泛的专家编排，不能仅依据节点名称推断已在所有版本中实现。当前中断主要位于基础图与 RCA 的 input 节点，SOP 使用自身的步骤循环。

![DoD 主图：目标架构](../assets/workbench-python/03-workflow.png)

[SVG 图](../assets/workbench-python/03-workflow.svg) · [Mermaid 图源](../assets/workbench-python/03-workflow.mmd)

### 7.3 LangGraph、LangChain 与平台的分工

LangGraph 管理外层业务图的状态和推进；LangChain 提供模型、消息、工具及专家 Agent 工具循环。当前专家使用 LangChain 的 `create_agent`，生成的 Agent 也基于 LangGraph 执行，因此这里存在“外层业务图 + 内层专家图”两层编排。[F1][F2]

![DoD 源码中的 LangGraph 与 LangChain 完整调用关系](../assets/workbench-python/19-langchain-langgraph.png)

[查看可缩放 SVG](../assets/workbench-python/19-langchain-langgraph.svg) · [查看 PNG](../assets/workbench-python/19-langchain-langgraph.png)

图按 A → B → C → D 展开 v16 的执行主线：A 固定请求配置并加载图，B 展示实际八节点链，C 放大 investigate 中的专家模型—工具循环，D 展示专家返回后如何校验、合成并持久化。E 单独说明外层与专家的检查点，F 对照 v17 的单节点直接问答。橙色虚线表示节点内部的展开关系；紫色回路表示工具结果以 ToolMessage 回到模型，模型可以继续查询，也可以结束。

图中最关键的源码连接是 `qa_v16.investigate → KnowledgeExpert.invoke → create_agent`。专家模型通过 `KnowledgeChatModel._agenerate → ModelInvoker → ChatDeepSeek` 调用模型；工具通过 `StructuredTool → PolicyToolExecutor → MCPAdapter` 调用知识源。专家返回后，外层图继续执行证据校验与摘录合成；其中 summary 模型调用复用 ModelInvoker，不创建第二个专家工具循环。

| 层次 | 本项目使用的接口 | 承担的职责 |
| --- | --- | --- |
| 外层 LangGraph | `StateGraph`、`add_node`、`add_edge`、`add_conditional_edges`、`compile` | 固定流程顺序、SOP 条件循环、图状态与检查点 |
| LangGraph 恢复接口 | `interrupt`、`Command(resume=...)`、`aget_state`、`ainvoke` | 等待用户、加载检查点、恢复可运行任务 |
| LangChain Agent | `create_agent`、`BaseChatModel`、`StructuredTool`、消息类型 | 在固定专家和工具集合内进行模型—工具循环 |
| 模型接入 | ModelFactory、`ChatDeepSeek`、ModelInvoker | 解析固定模型、校验部署凭据绑定、预算预留、调用、用量结算与结构化输出校验 |
| DoD 平台运行时 | Worker、Execution、PolicyToolExecutor、ResultProjector | 任务租约、取消和期限、当前授权、工具台账、证据与最终业务状态 |

以 v16 为例，实际调用关系为：

~~~text
RunService.create：固定 Profile、RunContext、QueryScope 和预算
  → Worker：领取 Job，创建受信 Execution
  → FoundationExecutor：解析图，加载或初始化外层状态
  → qa_v16.investigate
      → Worker.expert_execution(parent, 'knowledge')：签发专家子执行上下文
      → KnowledgeExpert.invoke(ExpertTask)
          → create_agent(KnowledgeChatModel, StructuredTool[], NamespacedSaver)
          → 模型提出 tool_calls
          → StructuredTool → PolicyToolExecutor → MCPAdapter → 已授权数据源
          → ToolMessage 返回 Agent，模型继续或结束
      → ExpertResult：subtask_id、text、evidence_refs
  → validate_evidence → compose_qa_outcome → finalize
  → ResultProjector → ResultService.finalize_in
~~~

这段关系中，`create_agent` 负责选择何时调用已经提供的工具；工具可见集合、业务范围、凭据和执行许可由平台确定。专家名称也来自 Worker 签发的上下文。v16 的协调者只能签发 knowledge 子上下文；v7/v8 可签发 knowledge 或 code。v5/v6 由 Worker 直接绑定相应专家身份。

#### 7.3.1 模型调用与预算

模型包装类的 `_agenerate` 不直接调用供应商 SDK，而是调用 `ModelInvoker.invoke`。Knowledge、Diagnosis、Code 分别使用 knowledge、diagnosis、code 模型角色；摘录合成和直接 QA 使用 summary 角色。角色最终解析为 RunContext 固定的模型版本。

ModelFactory 对环境变量凭据校验 team、agent、base_url 和 secret_ref 是否匹配部署凭据绑定；对管理端密钥校验团队、Agent 和地址的部署凭据授权，验证加密记录中的团队、模型、Agent 作用域和服务地址，并核对部署端点白名单，再构造 `ChatDeepSeek`，SDK 的 `max_retries` 为 0。ModelInvoker 在外部调用前预留预算并记录发送，在数据库事务外调用 `model.ainvoke`，之后结算用量；超时还受模型配置和 Run 剩余期限共同限制。可重试错误由统一调用层按配置处理。Worker 启用管理端密钥存储时，模型正文遥测关闭，仍可发送不含正文的链路和用量元数据。

结构化调用通过 `output_schema` 添加 JSON Schema 约束，并对模型文本执行 Pydantic 校验，必要时在剩余尝试额度内进行一次修复。工具调用与这种 JSON 输出模式不能在同一次 ModelInvoker 调用中组合。逻辑调用 ID 用于识别检查点重放中的已发送调用；遇到发送后响应不可恢复的情况，会保留不可用状态，避免重放时盲目重复发送。

#### 7.3.2 直接问答的请求与响应边界

浏览器提交 `POST /v1/runs`，API 固定 Agent 配置并持久化任务，返回 202；Worker 领取后才访问模型供应商。因此浏览器 Network 中的任务请求不是供应商原始请求，也不能由任务受理成功推断模型鉴权成功。

v18 的模型输入按以下顺序组装：平台基础约束 → 当前 Run 固定的 Agent 系统提示词和回复语言/格式 → 固定版本的 Skill 指令 → 授权且在窗口内的历史问答 → 当前问题。前三项合并为一个 system 消息；Agent 的 description 仅用于管理，不自动放入模型提示词。消息结构示意如下，SDK 负责实际序列化，模型名及采样、超时、输出限制以固定 ModelProfile 为准：

~~~json
{
  "model": "配置的模型名",
  "messages": [
    {"role": "system", "content": "平台约束、Agent 提示词、回复偏好和固定 Skill 指令"},
    {"role": "user", "content": "上一轮问题"},
    {"role": "assistant", "content": "上一轮回答"},
    {"role": "user", "content": "当前问题"}
  ]
}
~~~

无历史时省略中间两项，未绑定 Skill 时不追加技能内容。v18 使用 summary 模型角色进行直接文本调用，不因为配置了 Skill 就启用工具循环。Skill 注入细节见第 9.6 节。

非流式供应商响应通常包含 `choices[].message.content` 和 `usage`；SDK 将其归一为消息的 content、response_metadata、usage_metadata，ModelInvoker 再返回平台 ModelResult。工作流校验非空文本后写入 `outcome.summary`，并附加“以下内容由模型生成，未经外部知识源验证。”提示。页面读取持久化结果，不直接展示供应商原始 JSON；SDK 支持流式参数也不代表当前页面已逐 token 展示。401/403 等凭据错误映射为 `MODEL_CREDENTIAL_UNAVAILABLE`，不能伪造正常回答。当前日志能力与缺口见第 19.1 节。

### 7.4 专家 Agent 的输入、工具与结果

| 专家 | 实现与模型角色 | 当前工具能力 | 结果用途 |
| --- | --- | --- | --- |
| Knowledge | `knowledge.py` / knowledge | 经哈希与只读属性校验的已发布 ToolBinding；v16 选择 knowledge_search | 提供检索引用；自由文本不直接成为业务事实 |
| Diagnosis | `diagnosis.py` / diagnosis | 由相应图选定的受控诊断工具 | 提供诊断调查文本与引用；通用 v4 仍保留跨专家验证未完成项 |
| Code | `code.py` / code | 一个 `read_snapshot(path, start, end)` 工具，读取 Run 固定的单个仓库快照 | 提供固定 commit 和文件行证据；当前 CodeExpert 没有通用 Shell、Git 写入或脚本执行工具 |

专家任务的数据结构为 `ExpertTask(task_id, instruction)`，返回 `ExpertResult(subtask_id, text, evidence_refs)`。`subtask_id` 根据 Run ID、专家名和任务 ID 确定生成，专家检查点保存在 `expert:<subtask_id>` 命名空间，仍使用 Run ID 作为 LangGraph thread_id；该值与业务 Thread ID 分开使用。

专家 Agent 当前设置 `recursion_limit=12`，这是图执行步数上限，不等同于 12 次工具调用。Knowledge/Diagnosis 最多装配 4 个工具，并在工具执行时设置最多 4 个逻辑调用的限制；此外仍受 Run 总预算和平台权限约束。StructuredTool 暴露经筛选的参数 Schema，调用 ID 由框架注入；工具执行器还会依据权威发布 Schema 再校验参数，并从受信上下文补齐业务范围。

专家模型调用 ID 由子任务和消息摘要确定，工具调用 ID 由子任务和 tool_call_id 确定。恢复时，专家先读取命名空间内的检查点并核对原始任务内容；已有最终状态则复用，仍有可运行节点则继续执行。返回的证据引用从成功 ToolMessage 中提取、去重，不能通过最终自然语言自行编造。

v7/v8 当前通过 `for name in ('knowledge', 'code')` 依次调用两个专家，没有并行 fan-out。它们合并引用并保留各专家不可用或验证未完成的原因；单个专家失败不必然阻止另一个专家继续。更广泛的并行调查属于后续设计。

### 7.5 QA 与代码分析工作流

#### 7.5.1 知识 QA：dod.v16

入口要求 QueryScope 恰好包含 service、environment、region、start_at、end_at 五个非空字符串字段；时间窗必须大于 0 且不超过 3600 秒。时间可使用接口支持的标准日期时间字符串，或带时区的 ISO 时间；通过格式校验后，实际访问范围还受部署 grant 限制。

调查节点确认当前范围存在，从 Run 固定工具中读取已发布绑定，核对内容哈希，只选择只读的 knowledge_search；可用绑定数量要求为 1–4。随后在 knowledge 子执行上下文内运行 KnowledgeExpert。当前 `select_skills`、`collect_baseline` 两个节点不会额外检索 Skill 或采集监控信息。

EvidenceValidator 仅接受同时满足以下条件的引用：对应证据存在、scope_id 与当前范围一致、quality 为 valid、source 非空且引用内容可访问；必需的 investigate 步骤也必须完成。该校验通过不等同于业务问题已得到完整回答。

合成逻辑位于 `compose_qa_outcome`，分为以下边界：

1. 通过 `authorized_sources` 重新检查调用回执、证据来源、固定工具绑定和当前权限，取得允许读取的来源正文。
2. 只从已知证据格式提取文本。知识工具结果读取 matches 中的 content；来源截取有明确数量和长度上限。
3. 使用 summary 角色要求模型返回 `ExcerptSelection`，最多 8 条摘录、每条最多 1024 字符。发送前再次检查来源访问权限和 scope。
4. `validate_selection` 要求引用属于允许来源、原文是来源正文的精确子串、内容非空且没有重复；任一条无效则放弃该组摘录。
5. 合成后再次检查来源权限；只把仍有效的摘录写为 `status=observed` 的 findings，并保留 evidence_refs 和未完成项。

当前 v16 从 `partial_outcome` 构造结果，成功收集和摘录证据后仍保持 `completed=False`，最终业务状态为 partial。它提供可核对的来源材料；完整的自由生成知识结论尚未在该图中实现。配置步骤见 [知识问答配置](../../apps/agent/KNOWLEDGE_QA_SETUP.md)。

#### 7.5.2 直接模型 QA：dod.v17

v17 的 `DirectQAState` 仅含 run_id、input_text、outcome、phase；图中只有 answer 节点。节点先检查执行有效性和 thread_id，再复用 `_code_factory` 创建的 ModelInvoker，以 summary 角色发送 SystemMessage 和当前问题的 HumanMessage。虽然工厂名称包含 code，此处没有创建 CodeExpert 或调用仓库工具。

系统提示由基础约束、Run 固定的 Agent 系统提示词及回复偏好组成，要求直接回答，不能无依据声称已查询或验证外部来源。当前调用不传历史 Thread 消息、QueryScope、工具列表或检索结果；同一个业务 Thread 的历史消息不会在此节点自动形成多轮模型上下文。语言和格式仅是提示要求，`json` 不保证严格有效 JSON。

模型返回 status=ok、非空文本且长度不超过 65500 字符时，节点设置 `completed=True`、清空 unresolved_items，并添加“以下内容由模型生成，未经外部知识源验证。”前缀，正常投影后为 succeeded。模型能力缺失、预算或凭据不可用、已发送调用无法恢复等已处理情况保留部分结果及错误原因。配置步骤见 [直接模型问答配置](../../apps/agent/DIRECT_LLM_QA_SETUP.md)。

#### 7.5.3 代码与多专家：dod.v6–v8

v6 要求 code 模式、可用模型能力和恰好一个固定仓库引用。CodeExpert 通过 SnapshotReader 读取该快照中的有界文件行，形成仓库证据；当前结果仍提示需要进一步验证。v7 顺序追加知识检索与代码调查，去重合并证据；v8 在合并基础上增加原文摘录选择，输出仍保持 partial。

仓库证据必须指向 Run 固定快照，恢复时使用同一引用。Skill 资源和 Runner 脚本能力属于平台的其他边界，不能从当前 `read_snapshot` 工具推导出 CodeExpert 已拥有任意命令执行能力。

#### 7.5.4 多轮直接模型 QA：dod.v18

v18 仍是直接 LLM 问答，不依赖知识源。每次追问创建新的 Run，归属同一 Agent 和业务 Thread；图执行仍以新 run_id 隔离检查点和预算，而不是恢复上一轮 Run。模型收到当前 Run 固定的系统提示词、回复偏好、已绑定技能指令、筛选后的历史问答和当前问题。

- 只读取同团队、同 Agent、同 Thread 内，在当前 Run 受理前已完成的 v17/v18 成功结果；校验归属、消息与结果摘要，失败、部分完成或不一致记录不作为正常历史。
- `conversation_history_turns` 为 0–8 的整数，默认 8；一轮是一个问题加一个回答。它是最大值，不是保证传入的轮数。设为 0 只是不传历史，不会删除会话记录。
- 最多扫描 64 个候选 Run，历史正文上限 16000 字节；模型上下文窗口及 summary 角色 Token 预算可继续缩减历史。当前问题与必要指令仍须满足模型和预算约束。
- 显式会话请求存在未终结任务时返回 `THREAD_BUSY`；成功历史中含不兼容工作流时返回 `CONVERSATION_HISTORY_INCOMPATIBLE`。切换为多轮图不会把旧任务重新执行为模型问答。
- 系统提示词与 Skill 无法授予工具或密钥权限；v18 加载 Run 固定版本的 `SKILL.md` 文本指令，不执行脚本、工具或仓库操作。v17 不加载 Skill。当前没有自动摘要、跨会话长期记忆、全局 Agent 记忆或无限历史。

“上下文管理”是产品中的模型输入策略，与不可变执行配置对象 `RunContext` 不同，也不等于 LangGraph 检查点管理。实现见 [conversation_qa.py](../../apps/agent/src/workbench_agent/runtime/graphs/conversation_qa.py) 和 [agent_instructions.py](../../apps/agent/src/workbench_agent/runtime/graphs/agent_instructions.py)。

### 7.6 告警 RCA：范围、基线与提案

#### 7.6.1 范围确认

v9、v10 及合成 RCA 变体首先从告警输入解析服务、环境、时间锚点和窗口。`prepare` 将不含 region 的初始范围持久化；`input` 比较正文和链接中的 region。恰好有一个一致候选时直接继续，缺失或冲突时通过 `interrupt` 请求具名 region 输入。恢复只允许补充请求的 region，随后 `scope` 校验并持久化完整范围。

v9 到此直接生成“分析未完成”的结果；它没有指标、日志、Trace 基线采集步骤。v10/v13–v15 才继续下面的基线流程。

#### 7.6.2 基线绑定与采集

`bind_baseline` 使用受信 PolicyToolExecutor 生成基线计划，并要求各来源映射到一致的业务身份；将可用项目和业务范围写入检查点。`collect_baseline` 再次解析计划，要求当前 scope 和业务身份仍与绑定时一致，然后依次处理 metrics、logs、trace：

| 基线 | 工具 | 结果判定 |
| --- | --- | --- |
| metrics | metrics_query | 工具成功后仍检查证据、scope、quality 和 time_window |
| logs | logs_search | 同上；空结果与不可用分别记录 |
| trace | trace_get | 仅在计划允许且参数齐备时执行；缺失时保留 unavailable 等状态 |

每项工具调用使用稳定逻辑 ID，并带 expected_scope_id 和 expected_business_scope。只有工具返回 ok 且本地证据通过范围、质量和时间窗口检查，基线状态才变为 valid；范围过期、来源冲突、超时、拒绝及空结果都保留各自状态。当前采集循环为顺序执行。

`compose` 明确保留根因和业务健康尚未确立的未完成项。基线读取成功不会自动生成根因结论，也不会把业务判为正常。

#### 7.6.3 合成告警与动作提案

v13–v15 在每次执行有效性检查时还会重新校验来源绑定与 connector 策略。v14 的 propose 调用 `ActionService.propose_synthetic`，v15 调用 `propose_synthetic_batch`。该节点创建动作提案，后续确认、执行和回执属于 ActionService 与投递链路；完成 propose 不表示外部动作已执行。

第 8.2 节的跨知识、变更与代码调查属于通用目标流程；当前专用 RCA 图的确定能力是范围确认、基线采集及指定变体中的合成动作提案。

### 7.7 SOP 与巡检执行图

#### 7.7.1 工作流定义

`WorkflowDefinition` 是经过校验的数据定义，包含 1–32 个 WorkflowStep。当前步骤直接绑定一个只读工具，没有任意 Python 或自然语言 Agent 步骤类型。字段如下：

| 字段 | 当前含义与校验 |
| --- | --- |
| step_id、tool | 唯一步骤 ID 与已发布工具 alias |
| depends_on | 依赖的前置步骤；必须存在、不能重复，整图必须为 DAG |
| arguments | 固定参数 |
| inputs | `目标参数: 前置step_id.输出字段`；只能引用直接依赖，且不能覆盖同名固定参数 |
| required | 是否在汇总中报告该步骤未满足，默认 true |
| timeout_seconds | 单步时限，默认 20 秒，允许 1–60 秒 |
| failure_policy | stop 或 continue，默认 stop |
| condition | v12 支持；引用直接依赖输出字段，与有限 JSON 标量比较，可取反 |
| output_schema | v12 支持；限定为安全、有界的 JSON Schema 子集 |

发布/加载时校验 Schema、依赖和内容哈希。v11 入口会拒绝含 condition 或 output_schema 的定义；v12 才执行这两类约束。Skill 的自然语言 SOP 可以提供背景，强制步骤由 WorkflowVersion 的结构化定义表达。

#### 7.7.2 图如何推进步骤

`prepare` 校验固定工作流和工具，使用 TopologicalSorter 生成 order，初始化 index、results，并绑定 QueryScope 和业务授权范围。生产 MCP 执行路径要求步骤工具的业务范围能够一致验证。

`step` 每次只处理 `order[index]`，将结果保存到 `results[step_id]`，再递增 index。条件边根据剩余步骤与 missing 状态决定继续 step 或转 compose。因此当前实现是“DAG 定义 + 拓扑顺序串行执行”，没有并行分支合流。

执行前会重新检查历史证据可访问性、scope 与业务授权。依赖失败或已停止时，后续步骤记录 blocked；`failure_policy=stop` 会阻止后续实际工具调用，但索引仍推进以形成步骤记录。v12 条件不满足时记录 skipped；依赖状态允许 ok/skipped，若后续输入映射读取了被跳过步骤不存在的字段，仍会得到 invalid_input。v12 工具输出不满足 output_schema 时记录 invalid_output。

每个步骤通过版本名和 step_id 生成稳定调用 ID，调用受信工具执行器，并在派发前再次验证授权。`compose` 当前生成“执行需要复核”的 partial 结果，列出必需步骤的不合格状态；v12 中 skipped 不计为必需步骤失败。即使所有步骤执行成功，该图也没有自动推导业务健康或设置 completed=True。

### 7.8 调度、恢复和结果持久化

#### 7.8.1 定时巡检如何接入工作流

ScheduleService 只允许在支持 inspection 的 v11/v12 Profile 下创建计划，保存 WorkflowVersion 的版本/哈希、owner、五字段 cron、IANA 时区、输入及可选 QueryScope 模板。当前 overlap_policy 仅支持 forbid。

ScheduleScanner 使用数据库时钟扫描到期计划，在事务内复核状态、owner 权限、工作流版本和活动 Run。以 schedule_id + due_at 记录触发，并使用同一组合生成创建 Run 的幂等标识；每次启动独立的 inspection Run。可选范围模板以本次触发时刻为 end_at，按 window_seconds 回推 start_at，避免工具各自使用扫描时的当前时间。

延迟触发只在最近 6 小时内合并执行最近一次；更早触发记录 missed，已有活动任务则记录 overlap_skipped，权限或资源问题分别记录 denied/unavailable。next_due 使用 UTC 物理时刻：夏令时缺失的本地时间跳过，重复本地时间对应两个不同 UTC 时刻。Scheduler 负责创建任务，实际步骤仍由上述 SOP 图执行。

#### 7.8.2 检查点和人工恢复

FoundationExecutor 用 `thread_id=run_id`、`recursion_limit=128` 调用外层图。首次运行从已受理消息构造初始状态；已有检查点时检查 pending task，存在人工中断则等待合法 ResumeRequest，可运行任务则通过 `ainvoke(None, ...)` 继续。

基础 v2/v3 图的 input 节点可等待输入或确认，RCA input 节点可等待 region。InterruptExecutor 使用 `Command(resume=...)`，先以同步持久化运行到 input 后，确认 `last_applied_resume_id` 已保存，再标记恢复请求 applied 并继续图。节点内部代码可能重放，因此输入节点只应用响应和恢复标记；不在保存标记前执行下游外部操作。[F3]

v10、v11/v12、v13–v15 和 v17 的正常外层调用显式设置 `durability='sync'`；其他版本沿用调用默认持久化策略。专家 Agent 调用也显式使用同步持久化与独立命名空间。v16/v17 当前没有人工中断节点。

#### 7.8.3 图完成与 Run 完成

ResultProjector 从最新检查点读取 outcome，并使用 RunOutcome 校验。基础 v1–v3 的记录描述转换为 foundation.recorded.v1，明确说明仅记录输入或确认，没有执行模型推理和业务诊断。其他图使用节点产生的 outcome。

证据来源敏感的结果还会在投影链路重新检查当前权限和范围；权限变化时清除已失去授权的引用和派生文本。合成告警在 ResultService 的事务内额外执行来源检查与结果绑定。

最终由 `ResultService.finalize_in` 校验摘要和证据范围，在同一事务中保存结果、assistant 消息、最终事件并释放 Job：`completed=True` 投影为 succeeded，`completed=False` 投影为 partial。重复提交需匹配已保存摘要，避免产生第二份结果。图内名为 finalize 的节点、工具调用成功和外部动作完成是各自独立的执行边界。

### 7.9 代码阅读入口

以下路径相对 `apps/agent/src/workbench_agent/`：

| 关注点 | 代码入口 |
| --- | --- |
| 请求限制、固定配置与预算 | [application/runs.py](../../apps/agent/src/workbench_agent/application/runs.py) |
| 图注册、初始状态、调用和恢复 | [runtime/graphs/registry.py](../../apps/agent/src/workbench_agent/runtime/graphs/registry.py) |
| Worker 与受信专家身份 | [jobs/worker.py](../../apps/agent/src/workbench_agent/jobs/worker.py) |
| 三类专家 Agent | [knowledge.py](../../apps/agent/src/workbench_agent/runtime/experts/knowledge.py)、[diagnosis.py](../../apps/agent/src/workbench_agent/runtime/experts/diagnosis.py)、[code.py](../../apps/agent/src/workbench_agent/runtime/experts/code.py) |
| 模型构造和统一调用 | [factory.py](../../apps/agent/src/workbench_agent/runtime/models/factory.py)、[invoker.py](../../apps/agent/src/workbench_agent/runtime/models/invoker.py) |
| 工具与证据授权 | [tools/policy.py](../../apps/agent/src/workbench_agent/tools/policy.py)、[evidence_authority.py](../../apps/agent/src/workbench_agent/runtime/graphs/evidence_authority.py) |
| 知识 QA 与摘录验证 | [qa_v16.py](../../apps/agent/src/workbench_agent/runtime/graphs/qa_v16.py)、[qa_synthesis.py](../../apps/agent/src/workbench_agent/runtime/graphs/qa_synthesis.py)、[synthesis.py](../../apps/agent/src/workbench_agent/runtime/graphs/synthesis.py)、[validation.py](../../apps/agent/src/workbench_agent/runtime/graphs/validation.py) |
| 直接模型 QA | [direct_qa.py](../../apps/agent/src/workbench_agent/runtime/graphs/direct_qa.py) |
| 基线 RCA | [rca_baseline.py](../../apps/agent/src/workbench_agent/runtime/graphs/rca_baseline.py) |
| SOP 定义和执行 | [domain/workflows.py](../../apps/agent/src/workbench_agent/domain/workflows.py)、[sop_graph.py](../../apps/agent/src/workbench_agent/runtime/graphs/sop_graph.py) |
| 巡检调度 | [application/schedules.py](../../apps/agent/src/workbench_agent/application/schedules.py)、[jobs/schedules.py](../../apps/agent/src/workbench_agent/jobs/schedules.py)、[domain/schedules.py](../../apps/agent/src/workbench_agent/domain/schedules.py) |
| 最终结果转换与持久化 | [runtime/results.py](../../apps/agent/src/workbench_agent/runtime/results.py)、[application/results.py](../../apps/agent/src/workbench_agent/application/results.py) |

## 8 运行时序

### 8.1 业务答疑与异步交付

![任务运行与交付时序](../assets/workbench-python/04-runtime.png)

[SVG 图](../assets/workbench-python/04-runtime.svg) · [Mermaid 图源](../assets/workbench-python/04-runtime.mmd)

API 在数据库事务提交后返回 202，不等待模型结果。Worker 领取 Job 后加载固定配置，运行图和专家。图将 Outcome 写入检查点，Worker 随后调用 ResultProjector，ResultService 以幂等事务保存业务结果、消息和最终 RunEvent；外部动作如有 Outbox，投递由 Dispatcher 独立处理。客户端从 RunEvent 读取过程和结论。

Run 成功表示分析结果已持久化、必要的业务记录已提交、待投递动作已入 Outbox；不表示所有外部系统已经确认接收。外部投递/动作状态独立展示，只有收到回执才能显示 Jira 已创建或通知已送达。

### 8.2 告警 RCA

告警接入先验证来源，规范化区域、环境、服务、规则、标签和时间。若消息正文与链接参数冲突，保留冲突并查询权威告警详情；仍无法确定时进入等待输入，不能自行选取一个区域后继续。

RCA 先执行必需基线，再并行获取有限的知识与变更信息；Diagnosis 根据证据提出假设，必要时调用 Code 解释实现。validate_evidence 检查支持/反对证据和工具状态。预算不足时输出已有证据与待确认事项。历史结论可作为证据输入，复用必须核对团队、规则、服务、时间窗和环境，且记录复用来源。

### 8.3 人工补充与恢复

![输入与确认恢复时序](../assets/workbench-python/05-interrupt-resume.png)

[SVG 图](../assets/workbench-python/05-interrupt-resume.svg) · [Mermaid 图源](../assets/workbench-python/05-interrupt-resume.mmd)

1. 中断节点调用 interrupt，持久化图状态后返回等待载荷。
2. Worker 将中断投影为 interrupts 表、Run waiting 状态和 RunEvent，然后释放槽位与活动租约。
3. 用户提交 interrupt_id、expected_revision 和具名 response；API 核对身份、过期时间与参数摘要。
4. 事务保存 ResumeRequest，创建新 generation 的 Job，将 Run 变为 queued。客户端重发使用同一幂等键。
5. Worker 读取最新检查点。若等待点匹配，使用 Command(resume=...) 恢复；输入节点只更新状态，完成检查点前不执行外部写操作。
6. 输入节点把 resume_request_id 写入 last_applied_resume_id，并回到 prepare_context 校验业务范围；后续恢复发现该值时，不再次投递相同 Command。

如果图检查点已经保存中断，但业务 waiting 投影尚未写入就发生崩溃，协调器读取检查点并补齐投影。若检查点已越过输入节点但 ResumeRequest 尚未标记 applied，则依据 last_applied_resume_id 补齐状态。

LangGraph 中断恢复可能重新执行节点内代码，不能视作普通进程内暂停在下一行继续；中断节点必须保持可重入。[F3]

### 8.4 文件与脚本执行

Code Agent 或 Skill 工具提交结构化 Runner 请求，指定固定仓库快照、执行镜像、文件路径、脚本资源及参数。Runner 校验路径和资源绑定后执行，限制时间、输出和资源。结果包含退出码、stdout/stderr 摘要及 Artifact 引用；大输出不直接写入模型上下文。

Runner 先上传文件并校验摘要，再将 Artifact 标记 available。恢复时只复用 available 的产物；本地临时目录不能充当跨 Worker 恢复依据。

### 8.5 API 服务如何接收任务

普通业务请求从 `POST /v1/runs` 进入。API 负责验证身份和请求、固定本次执行配置、创建持久化任务并返回受理信息；模型和 LangGraph 的执行由后续 Worker 完成。

#### 8.5.1 服务与接口入口

阅读顺序：[`cli/api.py`](../../apps/agent/src/workbench_agent/cli/api.py) → [`api/app.py` 的 `create_app`](../../apps/agent/src/workbench_agent/api/app.py) → [`api/runs.py` 的 `create_run`](../../apps/agent/src/workbench_agent/api/runs.py) → [`application/runs.py` 的 `RunService.create`](../../apps/agent/src/workbench_agent/application/runs.py)。

`dod api` 使用 Uvicorn 启动 FastAPI。`create_app` 装配 SQLiteBackend、身份解析器和 RunService，注册 Run、事件等路由；启动时检查存储是否就绪。`create_run` 接收 FastAPI 校验后的 `CreateRun`、Bearer 身份和 `Idempotency-Key`，调用 `request.app.state.runs.create(...)`，然后将结果作为 HTTP 202 返回。

请求示例假定 Agent 已发布允许 QA 的 v17/v18 Profile，`agent_id` 和 Token 需替换为实际值；v16 还需要在 `business_context` 中提供合法的 `query_scope`：

```http
POST /v1/runs HTTP/1.1
Authorization: Bearer <token>
Idempotency-Key: qa-request-001
Content-Type: application/json

{
  "agent_id": "11111111-1111-4111-8111-111111111111",
  "mode": "qa",
  "input": {"type": "text", "text": "支付失败应该如何排查？"}
}
```

主要字段定义在 [`domain/runs.py`](../../apps/agent/src/workbench_agent/domain/runs.py)：

| 字段 | 用途 |
| --- | --- |
| `agent_id`、`thread_id` | 指定 Agent 和可选的已有会话；不传 `thread_id` 时创建新 Thread。 |
| `mode`、`input` | 指定任务类型和文本输入；mode 默认 `auto`，可选值包括 `qa`、`rca`、`code`、`sop`、`inspection`。当前创建 Run 不接受非空 `artifact_ids`。 |
| `business_context` | 承载业务上下文；例如 v16 QA 的 `query_scope` 会经过专门校验。 |
| `workflow_version_id` | SOP / inspection 请求需要指定已发布的工作流版本；其他模式不能随意携带。 |
| `budgets`、`summary_token_reserve` | 可请求更严格的执行预算并指定汇总额度，服务端检查 Agent 限制和模型上下文上限。 |
| `client_message_id`、`expected_revision` | 可选的来源消息去重标识、已有 Thread 的版本检查；它们与 HTTP 幂等键是不同机制。 |

#### 8.5.2 受理与入队过程

```mermaid
sequenceDiagram
    actor C as 客户端
    participant A as FastAPI 路由
    participant I as 身份与授权
    participant R as RunService
    participant DB as SQLite
    participant W as Worker
    C->>A: POST /v1/runs + Bearer + Idempotency-Key
    A->>I: 验证身份并解析团队成员
    A->>R: create(auth, CreateRun, key)
    R->>DB: 开始写事务，检查授权与幂等记录
    R->>DB: 校验 Thread、Profile、模式、预算与固定配置
    R->>DB: 写入 queued Run、消息、ready Job、事件和幂等记录
    R->>DB: 提交事务
    R-->>A: AcceptedRun
    A-->>C: 202 + run_id / thread_id / links
    W->>DB: 之后扫描并领取 Job
```

1. **身份与权限**：[`authenticate`](../../apps/agent/src/workbench_agent/api/auth.py) 解析 Bearer Token，由配置的 OIDC 或开发身份验证器验证，再结合数据库中的有效 Team 成员解析身份。`RunService.create` 在写事务内再次检查目标 Agent 当前存在且调用者至少具备 operator 权限；请求中的自报角色不作为授权依据。
2. **请求和版本检查**：FastAPI/Pydantic 校验字段类型；服务检查文本和其他元数据大小、Thread 归属和可选 `expected_revision`。随后读取 Agent 已发布 Profile，检查 mode 与图版本是否匹配、工作流版本和模型凭据是否可用，并计算预算。例如 v16 只接受 QA 且校验查询范围；v17/v18 只接受 QA，且不能携带 `query_scope`。受控的 v13–v15 告警图有独立接入限制。
3. **固定执行上下文**：`ContextResolver` 解析当前 Profile、图、模型及工具等版本，写入 Run 的 `versions`；预算和汇总 token 预留也在受理时确定。随后 Profile 更新不会把这个已受理 Run 静默切换到新版本，执行时仍会复核当前权限。
4. **一个事务内入库**：创建或复用 Thread，写入 `runs`（`queued`）、用户 `messages`、`run_jobs`（generation=1、kind=`run`、`ready`）、预算策略和 `run.accepted` 事件；同时保存幂等结果及审计记录。事务成功提交后，路由才返回 202；提交失败则不产生可领取的半套任务。
5. **客户端查看进度**：202 返回 `run_id`、`thread_id`、`status=queued`、`revision`、`accepted_at` 和 `links`。客户端可以通过 `GET /v1/runs/{run_id}` 查询，或连接 `GET /v1/runs/{run_id}/events` 接收事件；事件接口支持 `after_seq` 或 `Last-Event-ID` 续读。返回 202 表示已经受理，不表示分析已经完成。

同一个 `Idempotency-Key` 在相同 team、subject、agent 和路由范围内，72 小时内以相同请求重试会返回原受理结果，并带 `Idempotency-Replayed: true`；同一键搭配不同请求返回 `IDEMPOTENCY_CONFLICT`。重放前仍检查当前授权。可选 `client_message_id` 是另一层来源消息去重，范围和保存期不同，不能代替 HTTP 幂等键。

| 受理结果 | 示例 |
| --- | --- |
| 身份或权限不满足 | 无效 Bearer 返回 401；角色不足返回 403；资源不存在或不在可见范围时可返回 404。 |
| 请求不符合约束 | 字段非法返回 422；输入超出限制返回 413；模式与图、Thread 版本或幂等内容冲突可返回 409。具体错误码见服务端响应。 |
| 存储不可用 | 存储错误通常返回 503；未提交的事务不会被当作成功受理。 |

#### 8.5.3 其他任务来源

上述是普通 HTTP 请求链路。当前 [`api/ingress.py`](../../apps/agent/src/workbench_agent/api/ingress.py) 另有需显式配置的合成聊天/告警测试入口，校验来源后交由接入服务转换并受理；生产环境禁用该合成入口，不能据此认为真实外部连接器已经接入。定时巡检则由 [`jobs/schedules.py`](../../apps/agent/src/workbench_agent/jobs/schedules.py) 扫描计划后创建 Run，随后同样通过持久化 Job 交给 Worker。已进入人工等待的 Run 使用 `/v1/runs/{run_id}/resume` 提交回复并产生新的 resume Job，详见第 8.3 节。

### 8.6 Worker 如何领取、执行与恢复任务

本节以当前 SQLite 实现为准，说明代码实际如何运行；后续存储适配器可以改变争抢手段，但不能改变租约和恢复的语义。Run 是一次业务执行，Job 是与 Run 关联的一次持久调度单元，Worker 则是领取 Job 并执行图的进程。Scheduler 负责扫描期限、恢复候选和定时巡检；它可以领取恢复工作进行对账，但不运行普通模型任务。

#### 8.6.1 从入口到运行图

阅读顺序：[`cli/worker.py` 的 `run_worker`](../../apps/agent/src/workbench_agent/cli/worker.py) → [`Worker.run` / `run_once`](../../apps/agent/src/workbench_agent/jobs/worker.py) → [`JobService.claim`](../../apps/agent/src/workbench_agent/jobs/claim.py) → [`SQLiteJobStore.claim`](../../apps/agent/src/workbench_agent/persistence/backends/jobs.py) → [`FoundationExecutor.execute`](../../apps/agent/src/workbench_agent/runtime/graphs/registry.py)。

1. `dod worker` 最终进入 `run_worker`，创建 SQLiteBackend、图及执行器注册表，按配置装配模型、工具和管理任务；存储就绪后调用 `Worker.run()`。当前 CLI 使用 `worker-1` 作为 Worker ID，SQLite 部署以单 Worker 实例为基线。
2. `Worker.run()` 启动两个业务 Run slot 和一个独立管理 slot；每个 slot 执行一轮后默认等待 5 秒再扫描。管理 slot 领取管理 Job 前先检查是否有可领取的业务任务，管理 Job 使用独立租约和执行器。
3. 创建 Run 时，API 在同一写事务中保存 `queued` Run、输入消息和 generation=1、kind=`run`、status=`ready` 的 Job，提交后返回 202。Worker 不依赖 API 进程的内存来找到这个 Job。
4. 每个业务 slot 调用 `run_once()`；领取成功后建立 `Execution`，并发启动图执行协程与租约续期协程。Worker 根据 Run 固定的 `graph_version` 选择已注册的 executor 和 projector，不根据最新 Profile 临时更换图。
5. `FoundationExecutor` 以 `run_id` 作为 LangGraph 的 `configurable.thread_id`，加载检查点。首次执行从已保存的输入构造初始状态；已有可运行检查点时以 `ainvoke(None, ...)` 继续；有中断时不会自行编造回复。支持中断的版本由 `InterruptExecutor` 处理合法 ResumeRequest。实际图调用是 `graph.ainvoke(..., context=execution)`；专家 Agent、模型和工具由图节点在此过程中调用，见第 7.3 节。
6. 图返回后 Worker 再次检查所有权，在写事务中调用 projector。有效 Outcome 由 ResultService 保存为 `succeeded` 或 `partial`；中断投影为 `waiting_input` / `waiting_approval`。图中的节点完成不等于业务结果已经提交。

```mermaid
sequenceDiagram
    actor Client as 客户端
    participant API
    participant DB as SQLite / JobStore
    participant W as Worker slot
    participant G as LangGraph / Agent
    participant S as Scheduler / Reconciler
    Client->>API: 提交任务
    API->>DB: 事务写入 Run、输入、ready Job 和事件
    API-->>Client: 提交后返回 202
    W->>DB: 扫描并在短事务内领取，获得 Guard 和租约
    par 执行图
        W->>G: 按固定 graph_version 加载检查点并 ainvoke
        G->>DB: 在 Guard 保护下保存检查点
        G-->>W: 返回结果或中断
    and 维持所有权
        W->>DB: 默认每 10 秒检查取消、期限并续租
    end
    W->>DB: 校验 Guard，投影 Outcome 或中断，释放租约
    Note over DB,S: 如果 Worker 中途丢失，租约到期后先对账检查点
    S->>DB: 领取恢复所有权并决定投影、等待、继续或失败
```

#### 8.6.2 领取规则与所有权

`JobService.claim` 在短写事务内读取一页候选，最多 64 条，再按优先级和团队轮转尝试领取：`resume` / `recover` 优先，其次普通 Run、告警、定时任务。同一优先级按团队交替；连续无法领取时通过扫描游标继续检查后面的候选，能力变化时从头重新检查。游标只用于扫描，不代表已经获得任务。

| 检查项 | 当前实现 |
| --- | --- |
| 候选与并发 | `available_at` 已到，Job 为 `ready`、`retry_wait`、`capability_blocked`，或 `leased` 但租约已过期；在写事务中复读 Job revision 和状态。 |
| Worker 能力 | 普通执行需要已注册的固定 Graph 和 projector，并满足 Job 的 feature；纯投影恢复还需要 reconciler 和对应图能力。不满足时，未租出的 Job 标为 `capability_blocked`，以后扫描仍会重新检查。 |
| Team 与并发 | Team 必须 active，未达到 `execution_limit`；当前 SQLite 后端还限制全局最多两个有效业务执行租约。 |
| Thread 与等待 | Thread 的 `active_run_id` 必须为空或为当前 Run，且没有有效执行租约。被其他 Run 占用时，该候选延迟约 1 秒再查；处于等待状态的 Run 只能由 `resume` Job 领取。 |
| 原子提交 | 按 quota → Thread → Lease → Run → Job 的固定顺序更新：占用 Thread、建立 45 秒租约、递增 `run_epoch`、Job 变为 `leased`。普通执行递增 attempt 并写 `run.started`；纯投影恢复不增加执行 attempt。 |

领取返回的 `ExecutionGuard` 包含 team、thread、run、job、worker 和 `run_epoch`。它是所有权凭据，不是独立的永久权限：检查点、结果、事件等受保护写入必须在同一事务通过 LeaseGuard，核对当前 Job、Worker、epoch、租约有效期和允许的 Run 状态。新的 Worker 接管时 epoch 递增，因此旧 Worker 即使晚返回也不能继续投影或覆盖新结果。执行外部操作前还会检查取消、期限和当前授权；固定版本并不意味着权限永久有效。

默认每 10 秒续租，成功时将执行租约和 Job 租约各延长到当前时间加 45 秒。续租周期也检查期限与取消；存储错误或续租失败会令本次执行停止，Worker 取消执行协程。失去所有权后的释放或写入会被拒绝，后续由持有新 Guard 的恢复者处理。

#### 8.6.3 Run 和 Job 的状态流转

下面是常见路径，完整业务状态和动作状态见第 14 节；箭头说明一次变化可能由 API、Worker 或恢复服务完成，并非都由同一个进程执行。

```mermaid
stateDiagram-v2
    [*] --> queued: API 创建 Run
    queued --> running: Worker 领取普通 Job
    running --> waiting_input: 中断投影并释放租约
    running --> waiting_approval: 中断投影并释放租约
    waiting_input --> queued: 接受回复并创建 resume Job
    waiting_approval --> queued: 接受确认并创建 resume Job
    running --> finalizing: 进入结果收尾
    finalizing --> succeeded: 有效完整 Outcome 提交
    finalizing --> partial: 有效不完整 Outcome 提交
    running --> queued: 需要重试或恢复
    finalizing --> queued: 收尾恢复待调度
    running --> cancel_requested: 接受取消
    cancel_requested --> cancelled: 持有有效租约者完成取消
    queued --> cancelled: 取消排队任务
    waiting_input --> cancelled: 取消等待任务
    waiting_approval --> cancelled: 取消等待任务
    running --> failed: 不可安全恢复
    running --> timed_out: 活跃预算耗尽且无有效结果
    queued --> expired: 总生存期到期
    waiting_input --> expired: 等待或总生存期到期
    waiting_approval --> expired: 等待或总生存期到期
```

```mermaid
stateDiagram-v2
    [*] --> ready: 创建 run / resume / recover Job
    ready --> leased: 成功领取
    retry_wait --> leased: 到期后领取
    capability_blocked --> leased: 能力满足后领取
    leased --> retry_wait: 执行或投影需恢复
    leased --> capability_blocked: 领取后执行能力不可用
    leased --> done: 结果或等待状态已投影
    leased --> dead: 失败或活跃超时
    leased --> leased: 租约过期后以新 epoch 接管
    ready --> dead: 期限到达等控制终止
    ready --> done: 取消
```

这两张图展示主要路径而非所有控制终止组合。`queued` 的 Run 可能对应 `ready`、`retry_wait` 或 `capability_blocked` 的 Job；waiting 的 Run 对应已完成的当前 Job，释放租约和 Worker slot，但保留 Thread 的活动 Run 位置。终态释放 Thread 活动位置。当前 `ResultService.finalize_in` 在同一事务中设置 `finalizing` 并保存终态，正常提交时外部未必能单独观察到 `finalizing`。领取一个新的 Job generation 与重试计数不同：人工回复创建 `resume` generation 并开启新的执行片段；协调器确认可以继续时创建 `recover` generation，但沿用当前片段的尝试计数和 Run 总预算。

#### 8.6.4 容错、取消与期限

| 情况 | 当前处理 |
| --- | --- |
| 进程崩溃、租约过期 | Worker 或 Scheduler 可重新领取过期的 `leased` Job，获得新 epoch；这次先走 `projection_only`，由 Reconciler 读取检查点并判断，不直接重跑图。 |
| 图已经产生 Outcome，但投影失败 | 记录 `recovery_phase=projection` 和恢复意图；重新获得所有权后验证并投影已保存的 Outcome，避免把一次投影失败当成新分析。 |
| 已保存中断或回复标记 | Reconciler 可补齐 waiting 投影；若 `last_applied_resume_id` 已在检查点中，也可补记 ResumeRequest 的 applied 状态，避免再次提交同一回复。 |
| 尚有可运行节点 | Reconciler 核对检查点 ID、实际 next 节点、版本允许的节点和已有 Action 等事实。确认安全且片段未耗尽时，事务内结束旧 Job、创建新的 `recover` Job；状态无法解释或存在不能安全处理的副作用时标记 `STATE_RECONCILIATION_REQUIRED`。 |
| `InfrastructureFailure` | 当前执行片段前两次失败分别等待约 2 秒、10 秒，另加 0–0.5 秒随机抖动；达到 3 次后记录 `RETRY_EXHAUSTED`，由恢复链先检查已保存的进度。人工恢复开始新片段，不能刷新 Run 总预算。 |
| 执行器启动后异常、投影异常或协程取消 | 在仍持有所有权时释放租约，写入 `retry_wait` 和投影恢复意图；启动前能力缺失则可停在 `capability_blocked`。如果 Guard 已失效，旧 Worker 的释放也不会生效。 |
| 用户取消 | 排队或等待中的 Run 可直接终止；运行中的 Run 先变为 `cancel_requested`，Worker 检查后停止并在有效 Guard 下完成取消。已进入 `finalizing` 的 Run 拒绝新取消请求。 |
| 期限到达 | Scheduler 可直接终止没有活动租约的排队或等待 Run；运行中的终止需要先持有有效租约。总生存期到达优先记为 `expired`；活跃预算耗尽时，若已有可验证 Outcome 可先投影，否则记为 `timed_out`。 |

`Scheduler.tick()` 默认每 5 秒扫描期限、恢复候选和巡检触发；普通新 `ready` Job 留给 Worker 领取。恢复前会重新校验当前权限。对已经发出但结果未知的外部操作，检查点不提供跨外部系统的原子性，仍须按稳定调用身份、回执或对账处理，不能凭重放假设没有发生副作用。

#### 8.6.5 继续阅读源码

| 问题 | 入口 |
| --- | --- |
| 任务什么时候入队、人工回复如何产生新 Job | [`application/runs.py`](../../apps/agent/src/workbench_agent/application/runs.py)、[`application/resume.py`](../../apps/agent/src/workbench_agent/application/resume.py) |
| 如何分页、排序和争抢 | [`jobs/claim.py`](../../apps/agent/src/workbench_agent/jobs/claim.py)、[`jobs/fairness.py`](../../apps/agent/src/workbench_agent/jobs/fairness.py)、[`persistence/backends/jobs.py`](../../apps/agent/src/workbench_agent/persistence/backends/jobs.py) |
| 何时运行图、续租、投影或停止 | [`jobs/worker.py`](../../apps/agent/src/workbench_agent/jobs/worker.py)、[`runtime/graphs/registry.py`](../../apps/agent/src/workbench_agent/runtime/graphs/registry.py)、[`runtime/results.py`](../../apps/agent/src/workbench_agent/runtime/results.py) |
| 旧执行者为何不能写入 | [`domain/guards.py`](../../apps/agent/src/workbench_agent/domain/guards.py)、[`persistence/lease_guard.py`](../../apps/agent/src/workbench_agent/persistence/lease_guard.py) |
| 什么时候恢复、取消或超时 | [`jobs/reconciler.py`](../../apps/agent/src/workbench_agent/jobs/reconciler.py)、[`jobs/scheduler.py`](../../apps/agent/src/workbench_agent/jobs/scheduler.py)、[`jobs/deadlines.py`](../../apps/agent/src/workbench_agent/jobs/deadlines.py)、[`persistence/backends/controls.py`](../../apps/agent/src/workbench_agent/persistence/backends/controls.py) |

## 9 Skill 知识与 Agent

### 9.1 Skill 包与版本

Skill 资源包可从受控仓库或配置的资源来源导入；导入、校验、发布与加载代码位于 `apps/agent/src/workbench_agent/skill_management/`。Git 导入约定外部仓库中的 `skills/<skill-name>/`，不要求本项目根目录存在 `skills/`。资源包结构如下：

~~~text
skills/skill-name/
  SKILL.md
  references/
  scripts/
  assets/
~~~

当前 SkillVersion 保存版本号、内容哈希、包位置、状态与发布时间；适用场景等说明可以写入技能内容，不代表已有独立的工具依赖、运行镜像或输出约束配置字段。发布版本不可原地修改；回滚通过改变 Agent 的生效绑定并重新发布 Agent 实现。Run 固定版本，恢复时继续使用该版本；权限撤销仍即时生效。

完整资源包的目标加载顺序为目录摘要、选中的 SKILL.md、任务实际需要的 references/scripts；当前 v18 仅加载显式绑定的 `SKILL.md`，不自动执行引用资源。外部文档、用户消息和工具结果视为数据，不能覆盖运行权限或注册连接。当前导入校验拒绝不支持的 `.claude` Hook 等专有配置；转换适配属于后续工作，不能假定读取同一文件就得到同等执行语义。

### 9.2 知识检索

KnowledgeConnector 支持外部知识库工具和可替换 Retriever。P0 复用已经配置的数据源，不要求重建所有知识索引。每次检索记录 query、数据源、命中、返回状态和引用；服务不可用与知识未命中分开统计。

知识缺口只在真实检索轨迹存在时产生。后置评估失败不改变已经完成的主回答；费用记录归属主 Run 下的独立 evaluation 子任务。经验沉淀先形成候选记录，发布进团队知识或 Skill 需要既定审核流程。

### 9.3 Agent 与仓库

Agent 的 RuntimeProfile 固定模型、工作流、行为与上下文策略，以及仓库、Skill、工具版本引用；当前 ProfileDraft 没有独立运行镜像字段，镜像按部署与 Runner 契约管理。RepoSnapshot 直接归属 Agent，至少包含仓库标识、远端、branch/tag、commit SHA、准备状态和 Artifact/卷引用。

同一 Run 使用不可变 commit；仓库同步产生新 Snapshot，不在运行中的目录直接 git pull。大仓库可在 Runner 维护只读镜像及索引缓存，再为执行创建任务目录。敏感连接信息由 SecretProvider 注入。

### 9.4 Runner 边界

- 文件访问通过相对运行目录路径，解析后校验根目录，禁止符号链接越界。容器内 `/workspace` 保留为技术目录名，不构成 Workspace 产品或领域层。
- 脚本只能引用已发布 Skill 资源或授权的运行资源，参数为 argv 列表，避免拼接任意 Shell 字符串。
- 默认资源上限为 2 vCPU、2 GiB、单步骤 60 秒、单输出文件 20 MiB；Profile 可在管理员上限内调整。
- 子进程使用独立进程组；取消/超时终止整个进程组。网络与文件权限由运行镜像及容器策略约束。
- 用户凭据按调用作用域注入，不能修改共享 Python 进程的 os.environ 来切换用户。

### 9.5 团队共享 Skills 库与发布

Skills 是团队共享的能力资产，Agent 只绑定所需的已发布版本；不是在每个 Agent 内重复创建一份。共享条目使用 `skills.agent_id=NULL`，团队内可复用，跨团队不可见。原有 Agent 私有 Skill 导入能力保留，共享编辑器不将多文件资源包展平后覆盖。

| 环节 | 当前实现与约束 |
| --- | --- |
| 创建 | 稳定名称采用小写字母、数字和连字符，最长 128 字符；描述非空且最多 1000 字符；Markdown 正文非空且最多 32000 UTF-8 字节，API 同时限制字符数 |
| 草稿 | 编辑描述与 Markdown；名称创建后不改。草稿保存在 skill_drafts，使用 skills.revision 做 CAS；保存不改变已发布版本 |
| 发布 | 将 name/description 元数据与正文生成 SKILL.md，校验后写入不可变内容对象并新增 SkillVersion；每次发布产生新版本 |
| 绑定 | Agent 草稿最多选择 16 个不同 Skill，不允许同一技能同时绑定多个版本；固定 `{resource_id, version, content_hash}`，resource_id 指向 skill_version_id |
| 生效 | 添加或换版后须保存并发布 Agent；Skill 发布新版本不会自动升级任何 Agent，已受理 Run 保留原固定引用 |
| 权限 | viewer 读取，maintainer 创建与保存，admin 发布；连接器身份和限制到 Agent 范围的身份不能操作团队共享库 |

共享库支持列表查询、草稿详情、历史版本读取及版本内容回看。列表以 `limit`（1–100）、`after` 游标和可选 `q` 查询；当前历史版本接口一次返回该 Skill 的版本列表，不宣称已实现版本分页。

创建、保存和发布要求幂等键，保存与发布还要求 `If-Match` revision。发布先检查已提交的幂等回执，再准备对象；对象写入位于数据库写事务之外，最终事务重新核对授权与 revision 后登记版本。并发冲突不能覆盖较新草稿；冲突可能留下未引用的不可变对象，当前没有自动 GC。

当前使用本地对象适配器和绝对 `WORKBENCH_SKILL_OBJECT_ROOT`，由 API 写入、Worker 读取。这里没有新增 S3 Skill 适配器、远端 Git 拉取服务或独立 Skill 服务。操作与包导入边界见 [Skill 导入与共享库手册](../runbooks/skill-import.md)。

### 9.6 v18 的 Skill 加载与安全边界

v18 在模型调用前遍历 RunContext 固定的 Skill 引用，通过 SkillLoader 检查归属、当前权限、发布状态、内容哈希和对象完整性，读取每个 `SKILL.md`。内容按绑定顺序追加到同一个 system 消息中，并明确低于前面的平台及 Agent 指令；当前不进行语义路由或按问题动态挑选。

单次追加的 Skill 文本片段合计最多 65536 UTF-8 字节，且计入模型上下文及预算估算，可能缩小实际可携带的历史窗口。模型上下文不足仍应明确失败，不能静默换模型、跳过技能或突破预算。

缺少对象配置、版本不可用、对象损坏或授权失败时，以 `SKILL_UNAVAILABLE` 结束为部分完成，不在缺少所绑定技能的情况下继续模型调用。v17 不加载 Skill；v18 加载 Markdown 指令不等于执行 scripts、工具、仓库或外部检索。技能文本不能授予密钥、网络或工具权限，也不能作为任意代码执行入口。

### 9.7 Skill 的数据库和文件存储

一个团队共享 Skill 分别有管理身份、可编辑草稿和多个发布版本。SQLite 保存身份、草稿正文及版本引用；发布后的 `SKILL.md` 和清单保存在 Skill 对象目录，而不是以正文列存入 `skill_versions`。实际路径由部署配置的数据库 URL 和 `WORKBENCH_SKILL_OBJECT_ROOT` 决定；两个位置都属于持久数据。

| 存储位置 | 保存内容 | 使用方式 |
| --- | --- | --- |
| `skills` | team_id、skill_id、name、目录描述、状态和 revision；agent_id 为空表示团队共享 | 定位 Skill、检查团队归属与并发修订；`active` 表示管理身份可用，不代表已有发布版本 |
| `skill_drafts` | 共享 Skill 当前可编辑的 description 和 Markdown content | 保存后仍只是草稿；再次编辑会更新该行，不覆盖既有发布版 |
| `skill_versions` | skill_version_id、skill_id、version、目录内容 hash、bundle_ref 和状态 | 每次发布新增版本；按固定版本取得清单引用与完整性校验值 |
| `runtime_profiles` 和 RunContext | Agent 配置中的 Skill 版本引用，以及 Run 受理时固定的引用 | 绑定或回退须发布 Agent；旧 Run 不随草稿和新版本改变 |
| `skill-objects/` | 内容寻址的清单文件和 `SKILL.md` 等资源文件 | 文件名为对应文件内容的 SHA-256 十六进制值，按引用读取，不是以 Skill 名称命名的目录 |

新发布的单文档 Skill 的引用关系如下；同内容对象可以被复用，所以不能通过对象文件数量推算 Skill 或版本数量：

~~~text
skills.skill_id
  ├─ skill_drafts.content                    当前草稿正文
  └─ skill_versions                          不可变发布版本
       ├─ skill_version_id / version / hash  版本标识与目录摘要
       └─ bundle_ref: skill-manifest-v1:sha256:<清单文件哈希>
            └─ skill-objects/<清单文件哈希>   JSON 清单
                 └─ hashes["SKILL.md"]: <资源文件哈希>
                      └─ skill-objects/<资源文件哈希>  已发布 SKILL.md
~~~

清单包含 `format`、`name`、`description` 和 `hashes`；其中 `hashes` 将资源相对路径映射到各文件的摘要。`bundle_ref` 中的哈希标识清单文件，`hashes["SKILL.md"]` 标识技能正文文件，`skill_versions.hash` 是资源路径与摘要映射计算出的目录摘要；三者用途不同，不应互相替代。历史发布版本还可能引用旧 ZIP 包，不能假定所有 `bundle_ref` 都是新清单格式。

共享 Skill 发布时，平台以结构化 YAML 生成包含 `name`、`description` 的 `SKILL.md` 头部，再追加草稿 Markdown。对象存储按文件字节计算 SHA-256、写入不可变对象；读取时核对文件摘要、清单内容、版本目录摘要和归属。对象名看起来像目录，但实际是普通文件，可用只读工具查看，不能 `cd` 进入。不要手工编辑、重命名或删除对象；修改须通过 Skills 页面创建并发布新版本。SQLite 和对象目录须一起备份和恢复，备份 SQLite 应遵循第 16 节的一致性要求。

### 9.8 管理、发布与实际使用

1. 在 `/app/skills` 查询团队技能；搜索匹配名称或目录描述，不进行正文或向量检索。创建时填写名称、描述和 Markdown，系统在数据库中创建管理身份和草稿。
2. 编辑并保存草稿。保存按 `skills.revision` 进行并发检查，成功后递增 revision；旧发布版本保持原内容，草稿描述可能与列表中最近发布的目录描述不同。
3. 管理员显式发布。服务检查权限与幂等回执，验证草稿，生成 `SKILL.md`，将资源和清单写入对象目录；再在数据库事务中重新检查权限和 revision，新增 `skill_versions` 记录。提交后返回版本 ID、版本号和目录摘要。
4. 在 `/app/agents` 打开“添加 Skill”，搜索共享库并选择已发布版本，保存并发布 Agent。可显式换回旧版本；Skill 本身发布新版本不会自动更新已有 Agent。
5. 新建 Run 时固定 Agent 的版本引用。v18 Worker 从该引用读取指定 SkillVersion、清单与 `SKILL.md`，校验后将文本注入模型请求；不是根据用户问题动态搜索并选择 Skill。v17 不加载 Skill。

同一幂等请求的重放返回原发布结果，不重复新增版本；并发草稿变化导致 `REVISION_CONFLICT`，不能静默覆盖。对象目录不可用时发布失败；对象先写入而最终事务冲突时可能留下未引用文件，当前没有自动垃圾回收。权限、输入限制与 API 见第 9.5、11.2、20.3.1 节。

### 9.9 查询 Skill 数据的运维示例

以下示例用于有数据库访问权限的本地排查。先确认当前目录的 `agents-v1.sqlite3` 就是部署正在使用的数据库文件；不要使用错误的路径创建新的空数据库，也不要用 SQL 直接修改 Skill。SQLite CLI 可以尝试 `-readonly`；若本地只读打开失败而确认文件可正常打开，可按下例启用会话级 `query_only`。普通打开可能创建 WAL/SHM 辅助文件；`query_only` 只限制该连接后续 SQL 写入，不等同于操作系统级只读打开。

~~~bash
sqlite3 agents-v1.sqlite3
~~~

在 `sqlite>` 提示符下设置输出并查看技能身份：

~~~sql
PRAGMA query_only = ON;
.headers on
.mode column
SELECT team_id, skill_id, name, description, status, revision
FROM skills
ORDER BY team_id, name;
~~~

查看共享 Skill 的当前草稿，`content` 是草稿正文，不一定与某个已发布版本相同：

~~~sql
.mode line
SELECT team_id, skill_id, name, description, content
FROM skill_drafts
ORDER BY team_id, name;
~~~

查看发布版本、目录摘要及对象引用；如按名称筛选，仍须确认 team_id，因为不同团队可以有相同名称：

~~~sql
.mode column
SELECT s.team_id, s.name, v.skill_version_id,
       v.version, v.status, v.hash, v.bundle_ref
FROM skills AS s
JOIN skill_versions AS v
  ON v.team_id = s.team_id AND v.skill_id = s.skill_id
ORDER BY s.team_id, s.name, v.version DESC;
~~~

在 `sqlite>` 输入 `.quit` 退出。对于 `bundle_ref` 形如 `skill-manifest-v1:sha256:<摘要>` 的版本，可在终端用 `less "$WORKBENCH_SKILL_OBJECT_ROOT/<摘要>"` 只读查看清单，再用清单中的 `SKILL.md` 哈希读取对应文件；`<摘要>` 须替换成该版本真实值。优先使用 Skills 页的历史版本查看或 `GET /v1/skills/{skill_id}/versions/{version}`，由服务完成授权和完整性校验。直接查询 SQLite 不经过团队 API 授权，操作人员须遵循部署环境的数据访问权限；不要删除 WAL/SHM、使用 immutable 模式读取活动库或直接修改对象绕过校验。

## 10 工具与模型适配

### 10.1 ToolBinding

ToolBinding 定义 alias、provider_type、连接引用、真实工具名、输入/输出 Schema、团队/环境范围、read_only、timeout、retry_policy 和所需权限。Skill 引用 alias，连接及身份参数由平台注入。

Agent 中的 MCPAdapter 使用部署授权的连接发现和调用 MCP 工具；外层 PolicyToolExecutor 负责当前 Run 的授权、Schema、预算、执行台账和输出限制。连接生命周期按服务端/用户凭据作用域区分，不把不同用户的可变 Header 共用一个全局客户端。[F4]

`apps/mcp-server` 是独立服务，发布 knowledge_search、metrics_query、logs_search 和 trace_get 等只读工具，并对服务主体、资源元组和查询范围再次授权。Agent 当前装配 Streamable HTTP 客户端；服务端支持 Streamable HTTP 和 stdio。非回环连接须使用受信 HTTPS。两个边界的检查分别约束 Run 调用和数据源访问；本地合成链路已验证，真实企业数据源仍需验收。

工具返回结构统一包含 status、data、evidence_refs、artifacts、error 和 retryable。status 只能是 ok、empty、unavailable、denied、invalid_input、timeout 或 unknown。业务正常/异常必须来自 data 中的证据，不能从 HTTP 200 或空数组直接推断。

### 10.2 外部动作

Jira 创建、远端业务写入和消息通知先生成持久化 Action。确认与授权绑定具体 tool/version、目标范围和规范化参数哈希。Dispatcher 在执行前再次检查当前权限，并使用稳定 action_id 对接下游幂等机制。

下游不支持幂等键时，连接器必须有可核对的外部关联标识或结果查询；无法确认是否生效时标记 unknown，进入对账或人工处理，不能在超时后盲目再次执行。

### 10.3 模型配置

#### 10.3.1 配置位置与权威来源

一期使用 DeepSeek。LLM 配置分为部署初始化、版本化管理、Agent 绑定和运行解析四层：

| 层次 | 配置位置 | 内容与生效方式 |
| --- | --- | --- |
| 初始化 | config/local/agent.yaml 的 llm 段 | 首版默认模型与 Profile 种子；完整样例见第 18.1 节 |
| 密钥 | DEEPSEEK_API_KEY 环境变量，或管理端加密密钥存储 | 配置只保存 secret_ref，Worker 在授权后解析真实值；明文不写入数据库或检查点 |
| 模型管理 | model_profiles / model_profile_versions；一期存入 SQLite | endpoint、model_id、参数、能力和版本；发布版本不可原地修改 |
| Agent | 已发布 RuntimeProfile 的模型绑定 | 固定 default_model_profile_ref，及经授权的角色绑定 |
| Run | 创建时保存 model_profile_version_refs | Worker 与专家使用这些版本；恢复不重新解析最新配置 |

bootstrap 采用 create_if_missing：首次导入默认 DeepSeek Profile 并为新 Agent 绑定已发布版本；发现已有同名 Profile 时不覆盖。修改现有配置通过模型管理接口发布新版本，再发布 RuntimeProfile。编辑 YAML 或重启服务不能悄悄覆盖数据库中的已发布配置。

运行时唯一优先级为：RuntimeProfile 中显式发布的角色绑定 → Agent 默认模型绑定。部署默认只用于创建新 Agent；用户问题、Skill、普通 Run 请求和环境变量不会绕过已发布绑定切换模型。P0 router、knowledge、diagnosis、code、summary、evaluation 均继承默认绑定。

#### 10.3.2 ModelProfile 字段

| 字段 | 契约 |
| --- | --- |
| profile_id / name / version | 稳定身份、管理员可读名称、不可变发布版本 |
| provider / adapter | 一期为 deepseek，由注册表映射到 ChatDeepSeek；模型构建集中在 ModelFactory |
| protocol | 一期为 openai_chat_completions，协议与模型供应商分别记录 |
| base_url / model_id | 连接端点和真实模型调用名，均可配置并随版本固定 |
| secret_ref | 默认 env://DEEPSEEK_API_KEY；只有受信服务可解析 |
| temperature / max_output_tokens / max_context_tokens | 每次调用的参数与应用上限，仍受 Run 总预算限制 |
| request_timeout_seconds / streaming | 调用期限及是否使用增量输出 |
| sdk_max_retries / invocation_max_attempts | SDK 层为 0，统一 ModelInvoker 最多 3 次总尝试，避免叠加重试 |
| provider_options | 经过允许列表校验的供应商扩展参数，由适配器转换为请求字段 |
| structured_output_strategy | 一期 json_mode + Pydantic 校验，不把 JSON 合法等同 Schema 合法 |
| capabilities / verification_status | 文本、工具、流式、JSON、用量等探针记录，包含时间和验证版本 |
| pricing_version | 独立价格版本；未配置时费用为待核对，不能记作零 |

一期 ModelFactory 使用 langchain-deepseek 的 ChatDeepSeek 适配，不在每个 Agent 内直接读环境变量或创建 SDK 客户端。ModelInvoker 统一承担预算、重试、用量和 Trace。[F15]

#### 10.3.3 DeepSeek 首版默认值

官方直连配置使用 base_url=https://api.deepseek.com，示例 model_id=deepseek-flash；模型调用名与端点按实际环境配置。该示例依据 2026-11-12 的 DeepSeek 官方接口说明，不要求内部网关沿用同一模型名。[F14]

首版默认使用非 thinking 模式，通过 provider_options.thinking.type=disabled 显式指定；由适配器映射至供应商扩展请求。DeepSeek 当前支持工具调用，思考模式也有工具调用协议，不能依据旧 R1 文档一概判断为不支持。[F16][F18]

后续启用 thinking 时必须使用新的发布 Profile，验证工具轮次所需的 provider 字段、序列化与恢复语义；相关内部内容不进入用户 SSE、普通日志或报告。P0 的显式非 thinking 配置避免继承供应商默认模式而改变运行契约。

主 Agent 和专家的工具循环使用工具 Schema；最终结构化结果单独使用 JSON Output，并在提示中声明 JSON 与输出示例，再执行 Pydantic 验证。JSON 结果为空、只有空格或因输出上限截断时均视为无效；最多一次受预算限制的修正，仍失败则 partial/failed。首版不依赖 Beta strict 工具模式。[F17]

创建 Run 固定的是配置版本，不是供应商别名背后的模型权重。记录请求 model_id、响应中的模型字段和可取得的服务版本元数据；别名服务升级时不宣称可以复现完全相同的模型输出。

#### 10.3.4 模型检查与错误

启动/doctor 检查 Profile、端点、密钥引用、参数与适配器存在，不自动产生付费推理。管理员显式触发 probe，验证文本、真实工具调用循环、流式增量、JSON、用量和错误分类；探针不调用业务写工具。

密钥引用不可解析时，默认 Profile 不可执行，新 Run 返回 MODEL_PROFILE_UNAVAILABLE；运行中失效记录 MODEL_CREDENTIAL_UNAVAILABLE。模型名/端点无效、401/403、429、5xx、超时、空输出和 Schema 错误分别分类；请求/Trace 不能包含真实 API Key。P0 不静默切换到其他模型或供应商。

接入内部 DeepSeek 网关时，保持同一 Profile 和协议适配契约，另验收其工具、流式、JSON、取消和用量字段，不能以普通文本调用成功代替全部能力验证。未知用量或价格保留 usage_pending/cost_pending。

### 10.4 预算默认值

| Profile | 活跃执行预算 | 模型调用上限 | 工具尝试上限 | 总 Token 上限 |
| --- | --- | --- | --- | --- |
| qa | 180 秒 | 8 | 20 | 80,000 |
| rca | 300 秒 | 12 | 30 | 120,000 |
| code | 600 秒 | 12 | 30 | 120,000 |
| sop / inspection | 600 秒 | 12 | 40 | 120,000 |

Token 上限包括专家与评估的输入/输出；模型自身上下文上限另行校验。工具尝试包括实际失败重试，复用已有成功回执不计新尝试。父 Run 的预算通过数据库预留/结算服务控制，不能给每个专家独立发放完整预算。

compose_outcome 预留一次汇总的预算；预算已经耗尽时使用确定性模板输出已有证据和未完成项，不再超额调用模型。

等待输入默认 24 小时过期，Run 总墙钟上限为 48 小时；活跃预算只累计 Worker 实际执行区间。总墙钟或等待期限到达后转 expired，硬执行预算到达后按第 14 节处理。


## 11 核心对外接口

### 11.1 通用约定

- 前缀为 /v1，时间为带时区的 RFC 3339，服务端存储 UTC，展示和定时配置保留 IANA 时区。
- 身份来自 Bearer/OIDC 或连接器服务身份；team_id 从服务端授权上下文解析，不能相信请求体中的团队声明。
- 创建、恢复、取消和配置变更均要求 Idempotency-Key，长度 1–128。已有 Agent 的操作按团队、主体、Agent、路由和键保存请求哈希及原始响应 72 小时；创建 Agent 使用团队级作用域，不依赖任何现有活动 Agent。
- 同键同载荷返回原始响应并带 Idempotency-Replayed: true；同键不同载荷返回 409 IDEMPOTENCY_CONFLICT。
- UUID 用作资源标识。列表采用游标分页，默认 20、最大 100。资源不存在或属于无权访问的团队统一返回 404。
- 配置更新使用 If-Match/revision；Run 恢复使用 expected_revision。状态 revision 与 SSE seq 不同，文本增量不会改变状态 revision。

### 11.2 接口总览

| 方法与路径 | 用途 | 主要结果 |
| --- | --- | --- |
| POST /v1/threads | 创建业务会话 | 201，Thread |
| GET /v1/threads/{thread_id} | 会话和参与者范围 | 200 |
| GET /v1/threads/{thread_id}/messages | 对话消息分页 | 200 |
| POST /v1/runs | 创建 Run，可自动创建 Thread | 202，Run 受理结果 |
| GET /v1/runs/{run_id} | 状态、结果、等待信息及投递摘要 | 200 |
| GET /v1/runs/{run_id}/events | SSE 订阅及回放 | 200 text/event-stream |
| POST /v1/runs/{run_id}/resume | 补充信息或动作确认 | 202 |
| POST /v1/runs/{run_id}/cancel | 取消 | 202 或明确的终态结果 |
| GET /v1/runs/{run_id}/artifacts | 产物列表与受控下载地址 | 200 |
| POST /v1/runs/{run_id}/feedback | 用户反馈 | 201 |
| GET /v1/actions/{action_id} | 外部动作状态与回执 | 200 |
| POST /v1/actions/{action_id}/reconcile | 有权限的人工对账命令 | 202 |
| GET/POST /v1/teams | 团队列表与创建 | 200/201 |
| GET/PUT /v1/teams/{team_id}/members/{subject} | 成员与角色管理 | 200 |
| GET/POST /v1/model-profiles | 模型配置列表与草稿创建 | 200/201 |
| GET/PATCH /v1/model-profiles/{profile_id} | 读取或修改模型草稿 | 200 |
| POST /v1/model-profiles/{profile_id}/versions/publish | 校验并发布不可变模型版本 | 201 |
| POST /v1/model-profiles/{profile_id}/probe | 管理员显式模型能力探针 | 202 |
| GET/POST /v1/tool-bindings | 工具配置草稿管理 | 200/201 |
| POST /v1/artifact-uploads | 申请受控上传 | 201，上传标识 |
| POST /v1/artifact-uploads/{upload_id}/complete | 摘要验证后登记 Artifact | 200 |
| GET /v1/usage | 已授权团队的用量与费用 | 200 |
| GET/POST /v1/agents | Agent 列表与创建 | 200/201 |
| GET/PATCH /v1/agents/{agent_id} | 读取或更新配置草稿 | 200 |
| POST /v1/agents/{agent_id}/profiles/publish | 校验并发布运行配置 | 201，版本号 |
| POST /v1/agents/{agent_id}/repositories/sync | 准备新的仓库快照 | 202，准备任务 |
| GET/POST /v1/skills | 团队共享技能查询与创建草稿 | 200/201 |
| GET/PATCH /v1/skills/{skill_id} | 读取详情或保存描述与正文 | 200 |
| POST /v1/skills/{skill_id}/publish | 校验草稿并发布不可变版本 | 201 |
| GET /v1/skills/{skill_id}/versions | 已发布版本列表 | 200 |
| GET /v1/skills/{skill_id}/versions/{version} | 已发布版本内容 | 200 |
| POST /v1/tool-bindings/{binding_id}/probe | 配置/发现/调用分层探针 | 202 |
| GET/POST /v1/schedules | 定时配置与列表 | 200/201 |
| PATCH /v1/schedules/{schedule_id} | 修改、启停 | 200 |
| POST /v1/schedules/{schedule_id}/trigger | 手动触发，生成统一 Run | 202 |
| GET /v1/incidents | Duty Event 列表 | 200 |
| GET /v1/incidents/{incident_id} | 事件、证据与关联 Jira | 200 |
| POST /v1/incidents/{incident_id}/actions | 提议关联工单或跟进 | 202，Action |
| POST /v1/ingress/seatalk/{connector_id} | 消息回调与平台挑战 | 按连接器协议应答 |
| POST /v1/ingress/alerts/{connector_id} | 告警接入 | 202，受理/去重结果 |
| GET /health/live、/health/ready | 活性与就绪 | 200 或 503 |

管理接口由同一领域服务执行版本、授权和审计检查，不允许 API 绕过发布流程直接改写生效版本。同步仓库、原有私有 Skill 导入和探针属于持久化后台 Job，不执行于 HTTP 进程内存任务。团队共享 Skill 的有界文本编辑与发布是同步接口，不返回导入 Job；发布过程的对象写入和数据库事务边界见第 9.5 节。

### 11.3 创建 Run

~~~json
{
  "agent_id": "11111111-1111-4111-8111-111111111111",
  "thread_id": null,
  "mode": "rca",
  "input": {
    "type": "text",
    "text": "ID 支付成功率下降，请分析原因",
    "artifact_ids": []
  },
  "business_context": {
    "environment": "live",
    "region": "ID",
    "service": "payment-api",
    "anchor_time": "2026-11-12T08:00:00Z",
    "window_seconds": 1800
  },
  "response_language": "zh-CN",
  "client_message_id": "web-message-20261112-001"
}
~~~

mode 是 auto、qa、rca、code、sop、inspection 之一。显式 sop/inspection 还需要已发布 workflow_version_id。API 检查 Profile 是否允许该模式；客户端只能在服务器上限内降低预算，不能自行扩权。

~~~json
{
  "run_id": "22222222-2222-4222-8222-222222222222",
  "thread_id": "33333333-3333-4333-8333-333333333333",
  "status": "queued",
  "revision": 1,
  "accepted_at": "2026-11-12T08:00:01Z",
  "links": {
    "self": "/v1/runs/22222222-2222-4222-8222-222222222222",
    "events": "/v1/runs/22222222-2222-4222-8222-222222222222/events"
  }
}
~~~

数据库事务失败不得返回 accepted。未知客户端可以得到确认失败响应后使用原幂等键重试。

### 11.4 查询结果

GET Run 返回 status、revision、phase、attempt、created_at、started_at、active_elapsed_ms、waiting、outcome、usage、delivery_summary 和 links。outcome 中每个 finding 引用 evidence_id；Jira/通知等动作只展示真实 receipt。

waiting 示例：

~~~json
{
  "interrupt_id": "44444444-4444-4444-8444-444444444444",
  "kind": "user_input",
  "question": "请确认告警实际影响 ID 还是 PH",
  "required_fields": ["region"],
  "expires_at": "2026-11-13T08:01:00Z"
}
~~~

### 11.5 恢复输入和确认

~~~json
{
  "interrupt_id": "44444444-4444-4444-8444-444444444444",
  "expected_revision": 4,
  "response": {
    "kind": "user_input",
    "values": {"region": "ID"},
    "text": "以告警规则里的 ID 为准"
  }
}
~~~

动作确认使用同一个恢复入口，response.kind 为 approval，并提供 approval_id、decision（approve/reject）与 expected_args_hash。修改参数必须重新生成动作摘要和确认，不复用旧批准。actor 由认证上下文确定。

API 在一个事务中锁定 Run、Interrupt 和 ResumeRequest，检查等待点、revision、有效期和提交者权限；成功后保存输入并创建新的 Job generation。并发提交只有一个生效，其余返回原幂等响应或 409 INTERRUPT_ALREADY_RESOLVED。

### 11.6 取消

取消 queued、waiting_input 或 waiting_approval 时，若没有执行租约，可在事务内取消未执行 Job、关闭中断并转 cancelled。取消 running 时置 cancel_requested，通知 Worker 停止新的模型/工具操作并协作终止当前操作。

succeeded、partial、failed、cancelled、timed_out、expired 已终结；GET 返回当前状态。取消 finalizing 返回 409 FINALIZATION_STARTED，此阶段的业务提交优先完成，后续外部投递可以单独管理。取消 running 的响应是受理取消请求，不保证已经发生的外部动作可撤回。

### 11.7 错误格式

~~~json
{
  "error": {
    "code": "INTERRUPT_REVISION_CONFLICT",
    "message": "等待状态已经变化，请刷新后重试",
    "request_id": "req-20261112-001",
    "retryable": false,
    "details": {"current_revision": 5}
  }
}
~~~

| HTTP | 典型错误 | 客户端行为 |
| --- | --- | --- |
| 400/422 | INVALID_INPUT、SCHEMA_INVALID | 修正输入 |
| 401 | AUTHENTICATION_REQUIRED | 重新认证 |
| 403 | OPERATION_DENIED | 仅对已可见资源返回明确拒绝 |
| 404 | RESOURCE_NOT_FOUND | 不泄露跨团队资源存在性 |
| 409 | IDEMPOTENCY_CONFLICT、REVISION_CONFLICT、RUN_NOT_WAITING | 刷新状态或修正请求 |
| 410 | INTERRUPT_EXPIRED、EVENT_CURSOR_EXPIRED | 查看最终结果或启动新 Run |
| 413 | PAYLOAD_TOO_LARGE | 改为 Artifact 上传 |
| 429 | QUOTA_EXCEEDED | 遵循 Retry-After |
| 503 | NOT_READY、DEPENDENCY_UNAVAILABLE | 使用同一幂等键重试 |

Webhook 的响应遵循外部平台挑战/回调要求，但内部仍产生相同领域错误和审计事件。

## 12 内部接口与事件

### 12.1 内部模块契约

| 接口 | 输入 | 输出与保证 |
| --- | --- | --- |
| RunService.create | AuthContext、CreateRun、幂等键 | 原子写入消息、Run、Job、事件 |
| RunService.resume | AuthContext、Run、具名中断响应 | 创建唯一 ResumeRequest 与 Job generation |
| JobStore.claim | Worker 能力、租约、容量 | 同一 UnitOfWork 原子领取的 Job、ExecutionLease 与 run_epoch |
| GraphRegistry.resolve | graph_version、state_schema_version | 固定版本的可编译图 |
| ContextResolver.resolve | Run 和已授权 Agent | 不可变 RunContext 与版本引用 |
| SkillLoader.load | Context、版本引用、资源路径 | 校验过的内容和资源摘要 |
| PolicyToolExecutor.execute | RunContext、invocation_id、工具绑定、args | 已记录的 ToolResult |
| ExecutionExecutor.execute | Context、snapshot、镜像、argv、资源限额 | RunnerResult、Artifacts |
| BudgetService.reserve/settle | Run、调用类型、预估/实际用量 | 原子预留/结算；不可超发共享预算 |
| ActionService.propose/authorize | Context、动作、参数摘要、策略 | 持久化 Action 与确认状态 |
| ResultService.finalize | run_epoch、RunOutcome、结果摘要哈希 | 幂等提交结果、事件、Outbox |
| RunEventStore.append | 执行 Guard 或控制/交付授权、Run、type、payload | 同事务分配单调 seq 并持久化事件 |
| Reconciler.recover | 过期 Job、检查点和业务投影 | 原子接管后重建等待/完成状态或恢复 Job |
| CheckpointStore.put_with_guard | ExecutionGuard、typed payload、父链/pending writes | Guard 与快照在同一事务中生效 |
| StorageTransfer.export/import | Schema/Codec 版本、规范化记录及摘要 | 保留 ID、引用与二进制检查点的跨后端迁移包 |

上述契约只定义职责，后续实现使用 Pydantic DTO 和 Protocol；传输采用 HTTP 或进程内调用不改变语义。

### 12.2 RunEvent 协议

公共事件字段是 schema_version、run_id、seq、type、occurred_at、attempt、message_id、payload。seq 在单个 Run 内严格递增并持久化；多个 Run 之间不承诺顺序。

| type | payload 主要内容 |
| --- | --- |
| run.accepted / run.started | 模式、阶段、版本引用与执行尝试 |
| run.status_changed | status、revision 及状态变化原因 |
| phase.changed | 当前阶段及可读说明 |
| skill.loaded | Skill ID/版本，不含秘密内容 |
| tool.started / tool.completed | invocation_id、工具别名、受控输入摘要与状态 |
| evidence.added | evidence_id、来源与摘要 |
| assistant.delta | message_id、attempt、text；属于未定稿文本 |
| assistant.message_superseded | 旧 attempt/message_id 被重试替代 |
| input.required / approval.required | interrupt_id、问题/动作摘要、有效期 |
| run.resumed / run.recovery_scheduled | 原因、generation 和尝试 |
| run.completed / run.failed / run.cancelled / run.expired | 结果或错误摘要及结果链接 |
| action.updated / delivery.updated | 外部动作与投递状态 |

run.completed 的 payload.terminal_status 为 succeeded 或 partial；run.failed 的 terminal_status 为 failed 或 timed_out。取消和过期使用各自事件。

不向客户端暴露模型内部思考内容。过程事件呈现阶段、工具和证据摘要。最终答案使用独立消息标识，不能把早先的流式增量当作完成结果。

### 12.3 SSE 与回放

SSE 的 id 为 run_id:seq，event 为 type，data 为事件 JSON。客户端以 Last-Event-ID 或 after_seq 重连；同一事件重复到达时按 seq 去重。首次请求可从 seq=0 回放。

~~~text
id: 22222222-2222-4222-8222-222222222222:18
event: input.required
data: {"schema_version":"1","run_id":"22222222-2222-4222-8222-222222222222","seq":18,"type":"input.required","payload":{"interrupt_id":"44444444-4444-4444-8444-444444444444","required_fields":["region"]}}

~~~

API 从 RunEventStore 顺序查询当前后端中已提交的事件，Redis 只负责唤醒。Redis 通知丢失时最多 1 秒后继续查询。心跳每 15 秒发送 SSE 注释，不占持久化 seq。SQLite 首版文本增量按 250 毫秒或 256 个字符合并，先持久化再推送；后续后端可按容量调节。每批读取完成即关闭游标和读事务，再向 SSE 客户端发送，避免长读事务阻碍 WAL 回收。

事件统一保留 7 天；游标超过保留期返回 410 与 Run 结果链接，不宣称完整回放。慢客户端的进程缓冲最多 100 条，超出则断开并允许续传。SSE 断开不取消 Run；取消仅走取消接口或服务端执行期限。执行终态后，默认 SSE 在发送当前已提交事件后关闭；后续 Action/Delivery 更新可通过 GET Run/Action 或再次从游标订阅获得，不能把流结束解释为所有外部投递已经完成。

## 13 存储设计

### 13.1 存储边界、版本与关系

![核心存储关系](../assets/workbench-python/08-storage.png)

[SVG 图](../assets/workbench-python/08-storage.svg) · [Mermaid 图源](../assets/workbench-python/08-storage.mmd)

本节在 2026-11-16 字段基线上更新 Agent 领域命名：`workspaces` 表由 `agents` 替代，归属列统一为 `agent_id`，RunContext 使用 `agent_scope` 与 `agent_profile`，团队共享资源仍以 `agent_id=NULL` 表示。重构前的 `schema_v20.metadata`、59 张应用表/575 个字段和 `0021_synthetic_batch_policy` 仅是历史基线，不能据此判定新库就绪。新 Schema 导出、迁移头、存储协议和 Codec 以配套源码及 doctor 校验为准，重构验证状态见第 21.4 节；本文不以源码编号替代测试结果。

当前源码使用 `schema_agent_v3.metadata`、独立迁移头 `0003_run_trace_contexts`、存储协议 `dod-storage-agent-v1` 和 Codec `dod-checkpoint-v1`。`0002_shared_skills` 追加共享技能草稿表，`0003_run_trace_contexts` 追加异步 Trace 关联表；旧 Workspace 数据库仍不能沿此链升级。旧 `persistence/migrations/` 和 `schema_v1`–`schema_v20` 保留为历史。

第 13.2 节记录 Agent 基线的 59 张表、575 个字段，追加 skill_drafts（7 个字段）和 run_trace_contexts（4 个字段），当前合计 61 张应用表、586 个字段；这不代表相关业务已经接入或验收。第 13.8、13.9 节细化 JSON 与写入契约，第 13.10、13.11 节仍是后续加固与规划对象。本文不执行 DDL、不修改用户数据库。现有图表、HTML 和 ZIP 保留为历史快照，其中 Workspace 标签不覆盖本节新契约；图集重绘不属于本次源码契约验收。

| 存储层 | 权威内容 | 边界 |
| --- | --- | --- |
| 当前 StorageBackend | 身份/发布配置、Run/Job/Lease、消息/事件/中断、检查点、预算、Action/Outbox、审计 | 所有核心状态同后端同数据库；不存明文凭据或模型内部思考 |
| 对象存储/内容寻址目录 | Skill 资源、固定 commit 归档、原始证据、报告 | 数据库保存摘要、元数据和引用；临时路径不能作为恢复依据 |
| Redis | 唤醒、缓存、限流加速 | 不是唯一队列、锁、幂等记录或费用台账 |
| Worker/Runner 暂存 | 有界执行文件与上传暂存 | 不代表可下载产物，不作为长期事实源 |

**字段与约束约定**

- 字典中 `VARCHAR(n)/TEXT/BIGINT/BLOB` 是当前 SQLAlchemy 类型声明。SQLite 不自动保证 VARCHAR 长度；UUID 采用小写规范化 36 字符，但 repo_id、subject、connector_id 等业务键不因名称带 id 就变成 UUID。
- NULL 仅在列明“是”时允许；默认“—”表示没有显式 server_default，非空字段由调用方填写。ID 由服务生成，不使用数据库自增；历史时间戳默认 0 不代表有效创建时间，新写入必须显式填写。
- 标量时间统一 UTC 毫秒 BIGINT；JSON 内 start_at/end_at 为带时区 RFC 3339 字符串。0/1 标志也使用 BIGINT，由应用校验。
- PK、UK、FK、CHECK、索引列表是当前物理声明；未列出的逻辑关系没有数据库外键保护。FK 除另行标注外均 RESTRICT。状态列不是原生 Enum，JSON Schema 和状态转换由领域服务校验。
- 多数实体以全局资源 UUID 作 PK，并有 UK(team_id,资源ID) 供租户复合 FK 引用；查询仍必须带 team_id。部署策略单例和协议元数据是系统级例外。
- 同团队不等于同 Agent/Thread/Run；只有团队级 FK 的关联，还须在服务事务中校验更细归属。配置 JSON 引用也必须重验，不具有隐藏 FK。
- VersionRef 为 resource_id/string、version/string、content_hash/SHA-256；表内发布序号通常是 BIGINT，比较需严格转换，不能按字符串排序数字。

| 分组 | 当前表 |
| --- | --- |
| 团队与配置 | `teams`、`team_members`、`agents`、`runtime_profiles`、`model_profiles`、`model_profile_versions`、`skills`、`skill_versions`、`tool_binding_drafts`、`tool_bindings`、`repo_snapshots`、`workflow_versions` |
| 会话、运行和恢复 | `threads`、`messages`、`runs`、`run_jobs`、`execution_leases`、`run_events`、`interrupts`、`resume_requests`、`query_scope_revisions`、`run_outcomes` |
| 工具、证据和文件 | `tool_invocations`、`evidence`、`evidence_payloads`、`artifacts`、`artifact_upload_scopes`、`runner_cleanup_obligations` |
| 预算与费用 | `usage_budget_policies`、`usage_reservations`、`usage_records`、`usage_quarantine` |
| 动作、事件和巡检 | `actions`、`approvals`、`outbox`、`action_policy_state`、`incidents`、`incident_links`、`incident_action_links`、`schedules`、`schedule_fires`、`schedule_manual_fires` |
| 反馈、入口、幂等和审计 | `feedback`、`knowledge_gaps`、`idempotency_records`、`team_idempotency_records`、`source_events`、`audit_logs`、`admin_jobs`、`admin_job_events` |
| 合成验证与系统协议 | `synthetic_alert_sources`、`synthetic_result_bindings`、`synthetic_source_policy`、`synthetic_proposal_policy`、`synthetic_batch_policy`、`synthetic_action_policy_bindings`、`dod_storage_metadata` |
| 图执行检查点 | `graph_checkpoints`、`graph_pending_writes` |

### 13.2 当前逐表字段字典

每张表完整展开全部字段，不存在未注明的公共字段自动继承。字段顺序沿用元数据，不表示 SELECT * 是稳定公开接口。

#### 13.2.1 `teams`：团队与并发额度

领取/释放执行租约时同事务调整 active_executions。CHECK 仅保证非负，execution_limit 上限由领取事务保证；name 当前没有唯一约束。

| 字段 | 类型 | NULL | 数据库默认 | 含义 |
| --- | --- | --- | --- | --- |
| `team_id` | `VARCHAR(36)` | 否 | — | 所属团队 UUID；租户隔离边界，不由模型或工具输出改写。 |
| `name` | `VARCHAR(128)` | 否 | — | 资源名称；唯一性只以本表实际 UK 为准。 |
| `status` | `VARCHAR(32)` | 否 | — | 本表领域状态，具体语义见表说明及第 14 节；数据库不是原生 Enum。 |
| `active_executions` | `BIGINT` | 否 | `0` | 团队当前占用执行槽位数；与租约领取/释放同事务调整。 |
| `execution_limit` | `BIGINT` | 否 | `2` | 团队活动执行上限，默认 2，不是排队任务数上限。 |
| `revision` | `BIGINT` | 否 | `0` | 乐观并发版本，CAS 成功后递增；不等于内容发布序号。 |
| `created_at` | `BIGINT` | 否 | `0` | 创建 UTC 毫秒时间；服务显式填写 StorageClock，不把历史默认 0 当有效时间。 |
| `updated_at` | `BIGINT` | 否 | `0` | 最后持久变更 UTC 毫秒时间；由服务显式更新，无自动更新时间触发器。 |

- PK：`(team_id)`。
- UK：无额外唯一约束。
- FK：无物理外键。
- 显式索引：无；不含 PK/UK 自动索引。
- CHECK：`active_executions >= 0`。

#### 13.2.2 `team_members`：团队成员

每主体一行，role 为 viewer/operator/maintainer/admin 单值，status=active 时可授权；当前无独立 users/roles 表。

| 字段 | 类型 | NULL | 数据库默认 | 含义 |
| --- | --- | --- | --- | --- |
| `team_id` | `VARCHAR(36)` | 否 | — | 所属团队 UUID；租户隔离边界，不由模型或工具输出改写。 |
| `member_id` | `VARCHAR(36)` | 否 | — | 团队成员记录 UUID。 |
| `subject` | `VARCHAR(128)` | 否 | — | 身份系统主体字符串，不要求为 UUID，不存认证 Token。 |
| `role` | `VARCHAR(32)` | 否 | — | 团队角色或消息角色，允许值见本表说明。 |
| `status` | `VARCHAR(32)` | 否 | — | 本表领域状态，具体语义见表说明及第 14 节；数据库不是原生 Enum。 |
| `created_at` | `BIGINT` | 否 | `0` | 创建 UTC 毫秒时间；服务显式填写 StorageClock，不把历史默认 0 当有效时间。 |
| `updated_at` | `BIGINT` | 否 | `0` | 最后持久变更 UTC 毫秒时间；由服务显式更新，无自动更新时间触发器。 |

- PK：`(member_id)`。
- UK：`(team_id, member_id)`；`(team_id, subject)`。
- FK：`(team_id) → (teams.team_id)`，删除 RESTRICT。
- 显式索引：无；不含 PK/UK 自动索引。

#### 13.2.3 `agents`：Agent

先以 active_profile_id=NULL 插入，再插入 Profile 并更新指针，避免循环 FK；发布使用 Agent revision CAS。

| 字段 | 类型 | NULL | 数据库默认 | 含义 |
| --- | --- | --- | --- | --- |
| `team_id` | `VARCHAR(36)` | 否 | — | 所属团队 UUID；租户隔离边界，不由模型或工具输出改写。 |
| `agent_id` | `VARCHAR(36)` | 否 | — | 所属 Agent UUID；关联须同时校验团队和资源作用域。 |
| `name` | `VARCHAR(128)` | 否 | — | 资源名称；唯一性只以本表实际 UK 为准。 |
| `active_profile_id` | `VARCHAR(36)` | 是 | — | 当前生效 runtime_profiles.profile_id；未发布时为空。 |
| `status` | `VARCHAR(32)` | 否 | `'active'` | 本表领域状态，具体语义见表说明及第 14 节；数据库不是原生 Enum。 |
| `revision` | `BIGINT` | 否 | `0` | 乐观并发版本，CAS 成功后递增；不等于内容发布序号。 |
| `created_at` | `BIGINT` | 否 | `0` | 创建 UTC 毫秒时间；服务显式填写 StorageClock，不把历史默认 0 当有效时间。 |
| `updated_at` | `BIGINT` | 否 | `0` | 最后持久变更 UTC 毫秒时间；由服务显式更新，无自动更新时间触发器。 |

- PK：`(agent_id)`。
- UK：`(team_id, name)`；`(team_id, agent_id)`。
- FK：`(team_id) → (teams.team_id)`，删除 RESTRICT；`(team_id, active_profile_id) → (runtime_profiles.team_id, runtime_profiles.profile_id)`，删除 RESTRICT。
- 显式索引：无；不含 PK/UK 自动索引。

#### 13.2.4 `runtime_profiles`：运行配置草稿与发布版本

草稿 version=0/status=draft，发布新增正版本/status=published 并切换指针。profile_id 是该行独立 UUID。profile_json 是完整配置，*_versions 是一致投影；发布内容不原地修改。

| 字段 | 类型 | NULL | 数据库默认 | 含义 |
| --- | --- | --- | --- | --- |
| `team_id` | `VARCHAR(36)` | 否 | — | 所属团队 UUID；租户隔离边界，不由模型或工具输出改写。 |
| `profile_id` | `VARCHAR(36)` | 否 | — | 配置身份或版本 UUID，精确含义见本表说明。 |
| `agent_id` | `VARCHAR(36)` | 否 | — | 所属 Agent UUID；关联须同时校验团队和资源作用域。 |
| `version` | `BIGINT` | 否 | — | 资源内不可变内容发布序号；不与 CAS revision 混用。 |
| `graph_version` | `VARCHAR(128)` | 否 | — | 精确 Graph 实现版本，如 dod.v15。 |
| `model_versions` | `TEXT` | 是 | — | 默认和角色模型的 VersionRef 数组 JSON，草稿可空。 |
| `skill_versions` | `TEXT` | 是 | — | 已绑定 Skill 的 VersionRef 数组 JSON，草稿可空。 |
| `tool_versions` | `TEXT` | 是 | — | 已绑定工具 VersionRef 数组 JSON，草稿可空。 |
| `repo_versions` | `TEXT` | 是 | — | 不可变仓库快照的 VersionRef 数组 JSON，草稿可空。 |
| `profile_json` | `TEXT` | 否 | — | 完整 ProfileDraft 规范化 JSON，见 13.8。 |
| `hash` | `VARCHAR(64)` | 否 | — | 规范化版本内容的 SHA-256，小写十六进制；使用时核对内容。 |
| `status` | `VARCHAR(32)` | 否 | — | 本表领域状态，具体语义见表说明及第 14 节；数据库不是原生 Enum。 |
| `created_at` | `BIGINT` | 否 | `0` | 创建 UTC 毫秒时间；服务显式填写 StorageClock，不把历史默认 0 当有效时间。 |
| `updated_at` | `BIGINT` | 否 | `0` | 最后持久变更 UTC 毫秒时间；由服务显式更新，无自动更新时间触发器。 |

- PK：`(profile_id)`。
- UK：`(team_id, profile_id)`；`(agent_id, version)`。
- FK：`(team_id) → (teams.team_id)`，删除 RESTRICT；`(team_id, agent_id) → (agents.team_id, agents.agent_id)`，删除 RESTRICT。
- 显式索引：无；不含 PK/UK 自动索引。

#### 13.2.5 `model_profiles`：模型管理身份与草稿

profile_id 为模型身份；agent_id=NULL 时 scope=team，非空时 scope=agent，资源归属必须匹配。active_version 只有逻辑关联；当前未声明模型名唯一性。

| 字段 | 类型 | NULL | 数据库默认 | 含义 |
| --- | --- | --- | --- | --- |
| `team_id` | `VARCHAR(36)` | 否 | — | 所属团队 UUID；租户隔离边界，不由模型或工具输出改写。 |
| `profile_id` | `VARCHAR(36)` | 否 | — | 配置身份或版本 UUID，精确含义见本表说明。 |
| `agent_id` | `VARCHAR(36)` | 是 | — | 所属 Agent UUID；关联须同时校验团队和资源作用域。 |
| `name` | `VARCHAR(128)` | 否 | — | 资源名称；唯一性只以本表实际 UK 为准。 |
| `scope` | `VARCHAR(32)` | 否 | — | 模型范围/工具范围/动作 Scope，三者物理语义见本表说明。 |
| `draft` | `TEXT` | 否 | — | 完整 ModelProfile 可编辑草稿 JSON，见 13.8。 |
| `active_version` | `BIGINT` | 是 | — | 同模型 profile_id 的当前发布序号；当前只有逻辑关联。 |
| `revision` | `BIGINT` | 否 | `0` | 乐观并发版本，CAS 成功后递增；不等于内容发布序号。 |
| `created_at` | `BIGINT` | 否 | `0` | 创建 UTC 毫秒时间；服务显式填写 StorageClock，不把历史默认 0 当有效时间。 |
| `updated_at` | `BIGINT` | 否 | `0` | 最后持久变更 UTC 毫秒时间；由服务显式更新，无自动更新时间触发器。 |

- PK：`(profile_id)`。
- UK：`(team_id, profile_id)`。
- FK：`(team_id) → (teams.team_id)`，删除 RESTRICT；`(team_id, agent_id) → (agents.team_id, agents.agent_id)`，删除 RESTRICT。
- 显式索引：无；不含 PK/UK 自动索引。

#### 13.2.6 `model_profile_versions`：不可变模型版本

model_version_id 是 VersionRef.resource_id；标量 provider/adapter/base_url/model_id 等必须与 limits 中完整 ModelProfile 一致。capabilities 保存能力记录，未探测不伪造支持全部能力。

| 字段 | 类型 | NULL | 数据库默认 | 含义 |
| --- | --- | --- | --- | --- |
| `team_id` | `VARCHAR(36)` | 否 | — | 所属团队 UUID；租户隔离边界，不由模型或工具输出改写。 |
| `model_version_id` | `VARCHAR(36)` | 否 | — | 不可变模型发布版本 UUID，模型 VersionRef.resource_id 使用此值。 |
| `profile_id` | `VARCHAR(36)` | 否 | — | 配置身份或版本 UUID，精确含义见本表说明。 |
| `version` | `BIGINT` | 否 | — | 资源内不可变内容发布序号；不与 CAS revision 混用。 |
| `provider` | `VARCHAR(128)` | 否 | — | 模型/工具提供者标识，按本表配置校验。 |
| `adapter` | `VARCHAR(128)` | 否 | — | 模型适配器标识，首版 deepseek。 |
| `protocol` | `VARCHAR(128)` | 否 | — | 模型调用协议，首版 openai_chat_completions。 |
| `base_url` | `TEXT` | 否 | — | 不含认证信息的 HTTPS 模型地址。 |
| `model_id` | `VARCHAR(128)` | 否 | — | 供应商请求模型名/别名，不表示底层权重可复现。 |
| `secret_ref` | `TEXT` | 否 | — | env:// 或 secret:// 凭据引用；绝不存 Token 明文。 |
| `limits` | `TEXT` | 否 | — | 完整 ModelProfile JSON，含端点、参数和重试限制，不是只有额度，见 13.8。 |
| `capabilities` | `TEXT` | 否 | — | 版本化能力/执行要求 JSON；具体内容见本表说明。 |
| `hash` | `VARCHAR(64)` | 否 | — | 规范化版本内容的 SHA-256，小写十六进制；使用时核对内容。 |
| `created_at` | `BIGINT` | 否 | `0` | 创建 UTC 毫秒时间；服务显式填写 StorageClock，不把历史默认 0 当有效时间。 |
| `updated_at` | `BIGINT` | 否 | `0` | 最后持久变更 UTC 毫秒时间；由服务显式更新，无自动更新时间触发器。 |

- PK：`(model_version_id)`。
- UK：`(profile_id, version)`；`(team_id, model_version_id)`。
- FK：`(team_id) → (teams.team_id)`，删除 RESTRICT；`(team_id, profile_id) → (model_profiles.team_id, model_profiles.profile_id)`，删除 RESTRICT。
- 显式索引：无；不含 PK/UK 自动索引。

#### 13.2.7 `skills`：Skill 管理身份

agent_id=NULL 表示团队共享；非空时限对应 Agent。skills 表本身未声明名称唯一性；共享编辑库由第 13.2.60 节的 skill_drafts 约束团队内名称唯一。管理状态与发布版本状态分离。

| 字段 | 类型 | NULL | 数据库默认 | 含义 |
| --- | --- | --- | --- | --- |
| `team_id` | `VARCHAR(36)` | 否 | — | 所属团队 UUID；租户隔离边界，不由模型或工具输出改写。 |
| `skill_id` | `VARCHAR(36)` | 否 | — | Skill 管理身份 UUID。 |
| `agent_id` | `VARCHAR(36)` | 是 | — | 所属 Agent UUID；关联须同时校验团队和资源作用域。 |
| `name` | `VARCHAR(128)` | 否 | — | 资源名称；唯一性只以本表实际 UK 为准。 |
| `description` | `TEXT` | 是 | — | Skill 目录级说明，不等于完整 SKILL.md。 |
| `status` | `VARCHAR(32)` | 否 | — | 本表领域状态，具体语义见表说明及第 14 节；数据库不是原生 Enum。 |
| `revision` | `BIGINT` | 否 | `0` | 乐观并发版本，CAS 成功后递增；不等于内容发布序号。 |
| `created_at` | `BIGINT` | 否 | `0` | 创建 UTC 毫秒时间；服务显式填写 StorageClock，不把历史默认 0 当有效时间。 |
| `updated_at` | `BIGINT` | 否 | `0` | 最后持久变更 UTC 毫秒时间；由服务显式更新，无自动更新时间触发器。 |

- PK：`(skill_id)`。
- UK：`(team_id, skill_id)`。
- FK：`(team_id) → (teams.team_id)`，删除 RESTRICT；`(team_id, agent_id) → (agents.team_id, agents.agent_id)`，删除 RESTRICT。
- 显式索引：无；不含 PK/UK 自动索引。

#### 13.2.8 `skill_versions`：不可变 Skill 版本

运行只允许 published。bundle_ref 支持 skill-manifest-v1:sha256:<摘要> 和历史包；hash 按导入格式解释，不能替换成单个资源摘要。

| 字段 | 类型 | NULL | 数据库默认 | 含义 |
| --- | --- | --- | --- | --- |
| `team_id` | `VARCHAR(36)` | 否 | — | 所属团队 UUID；租户隔离边界，不由模型或工具输出改写。 |
| `skill_version_id` | `VARCHAR(36)` | 否 | — | 不可变 Skill 发布版本 UUID。 |
| `skill_id` | `VARCHAR(36)` | 否 | — | Skill 管理身份 UUID。 |
| `version` | `BIGINT` | 否 | — | 资源内不可变内容发布序号；不与 CAS revision 混用。 |
| `hash` | `VARCHAR(64)` | 否 | — | 规范化版本内容的 SHA-256，小写十六进制；使用时核对内容。 |
| `bundle_ref` | `TEXT` | 否 | — | 内容寻址 Skill 清单或受支持历史 ZIP 引用。 |
| `status` | `VARCHAR(32)` | 否 | — | 本表领域状态，具体语义见表说明及第 14 节；数据库不是原生 Enum。 |
| `created_at` | `BIGINT` | 否 | `0` | 创建 UTC 毫秒时间；服务显式填写 StorageClock，不把历史默认 0 当有效时间。 |
| `updated_at` | `BIGINT` | 否 | `0` | 最后持久变更 UTC 毫秒时间；由服务显式更新，无自动更新时间触发器。 |

- PK：`(skill_version_id)`。
- UK：`(skill_id, version)`；`(team_id, skill_version_id)`。
- FK：`(team_id) → (teams.team_id)`，删除 RESTRICT；`(team_id, skill_id) → (skills.team_id, skills.skill_id)`，删除 RESTRICT。
- 显式索引：无；不含 PK/UK 自动索引。

#### 13.2.9 `tool_binding_drafts`：工具配置草稿

一 Agent 一 alias 一行；payload 是完整 ToolBinding JSON。发布新增 tool_bindings 行，不修改旧版本。

| 字段 | 类型 | NULL | 数据库默认 | 含义 |
| --- | --- | --- | --- | --- |
| `team_id` | `VARCHAR(36)` | 否 | — | 所属团队 UUID；租户隔离边界，不由模型或工具输出改写。 |
| `draft_id` | `VARCHAR(36)` | 否 | — | 工具配置草稿 UUID。 |
| `agent_id` | `VARCHAR(36)` | 否 | — | 所属 Agent UUID；关联须同时校验团队和资源作用域。 |
| `alias` | `VARCHAR(128)` | 否 | — | Agent 内稳定工具别名；发布后通过精确版本引用。 |
| `payload` | `TEXT` | 否 | — | 版本化正文；具体格式和可信边界见本表说明及 13.8。 |
| `revision` | `BIGINT` | 否 | — | 乐观并发版本，CAS 成功后递增；不等于内容发布序号。 |
| `created_at` | `BIGINT` | 否 | — | 创建 UTC 毫秒时间；服务显式填写 StorageClock，不把历史默认 0 当有效时间。 |
| `updated_at` | `BIGINT` | 否 | — | 最后持久变更 UTC 毫秒时间；由服务显式更新，无自动更新时间触发器。 |

- PK：`(draft_id)`。
- UK：`(team_id, draft_id)`；`(team_id, agent_id, alias)`。
- FK：`(team_id, agent_id) → (agents.team_id, agents.agent_id)`，删除 RESTRICT。
- 显式索引：无；不含 PK/UK 自动索引。

#### 13.2.10 `tool_bindings`：不可变工具绑定

schema 实际存完整 ToolBinding JSON，scope 是范围投影，auth_ref 只保存作用域/引用；每次执行仍检查当前部署权限及 QueryScope。

| 字段 | 类型 | NULL | 数据库默认 | 含义 |
| --- | --- | --- | --- | --- |
| `team_id` | `VARCHAR(36)` | 否 | — | 所属团队 UUID；租户隔离边界，不由模型或工具输出改写。 |
| `binding_id` | `VARCHAR(36)` | 否 | — | 某个不可变工具绑定版本 UUID。 |
| `agent_id` | `VARCHAR(36)` | 否 | — | 所属 Agent UUID；关联须同时校验团队和资源作用域。 |
| `alias` | `VARCHAR(128)` | 否 | — | Agent 内稳定工具别名；发布后通过精确版本引用。 |
| `provider` | `VARCHAR(128)` | 否 | — | 模型/工具提供者标识，按本表配置校验。 |
| `schema` | `TEXT` | 否 | — | 工具完整配置或中断描述 JSON，不是必然只有输入 JSON Schema。 |
| `scope` | `TEXT` | 否 | — | 模型范围/工具范围/动作 Scope，三者物理语义见本表说明。 |
| `auth_ref` | `TEXT` | 是 | — | 凭据作用域或引用投影，不是明文凭据。 |
| `version` | `BIGINT` | 否 | — | 资源内不可变内容发布序号；不与 CAS revision 混用。 |
| `hash` | `VARCHAR(64)` | 否 | — | 规范化版本内容的 SHA-256，小写十六进制；使用时核对内容。 |
| `created_at` | `BIGINT` | 否 | `0` | 创建 UTC 毫秒时间；服务显式填写 StorageClock，不把历史默认 0 当有效时间。 |
| `updated_at` | `BIGINT` | 否 | `0` | 最后持久变更 UTC 毫秒时间；由服务显式更新，无自动更新时间触发器。 |

- PK：`(binding_id)`。
- UK：`(team_id, binding_id)`；`(agent_id, alias, version)`。
- FK：`(team_id) → (teams.team_id)`，删除 RESTRICT；`(team_id, agent_id) → (agents.team_id, agents.agent_id)`，删除 RESTRICT。
- 显式索引：无；不含 PK/UK 自动索引。

#### 13.2.11 `repo_snapshots`：不可变 Git 快照

运行仅使用 ready 快照。repo_id 为逻辑仓库键，当前 VARCHAR(36) 与部署配置最大 128 存在长度差异，见 13.10。归档哈希通过 storage_ref 和 VersionRef 保留。

| 字段 | 类型 | NULL | 数据库默认 | 含义 |
| --- | --- | --- | --- | --- |
| `team_id` | `VARCHAR(36)` | 否 | — | 所属团队 UUID；租户隔离边界，不由模型或工具输出改写。 |
| `snapshot_id` | `VARCHAR(36)` | 否 | — | 不可变仓库快照 UUID。 |
| `agent_id` | `VARCHAR(36)` | 否 | — | 所属 Agent UUID；关联须同时校验团队和资源作用域。 |
| `repo_id` | `VARCHAR(36)` | 否 | — | 部署映射的逻辑仓库键，不保证为 UUID；当前列长度仅 36，见 13.10。 |
| `commit_sha` | `VARCHAR(64)` | 否 | — | 完整 40/64 位 Git commit，不能用可移动 branch/tag 代替。 |
| `format_version` | `VARCHAR(128)` | 否 | — | 仓库归档格式版本。 |
| `storage_ref` | `TEXT` | 否 | — | 内容寻址仓库归档引用，不能指向临时工作目录。 |
| `status` | `VARCHAR(32)` | 否 | — | 本表领域状态，具体语义见表说明及第 14 节；数据库不是原生 Enum。 |
| `created_at` | `BIGINT` | 否 | `0` | 创建 UTC 毫秒时间；服务显式填写 StorageClock，不把历史默认 0 当有效时间。 |
| `updated_at` | `BIGINT` | 否 | `0` | 最后持久变更 UTC 毫秒时间；由服务显式更新，无自动更新时间触发器。 |

- PK：`(snapshot_id)`。
- UK：`(repo_id, commit_sha, format_version)`；`(team_id, snapshot_id)`。
- FK：`(team_id) → (teams.team_id)`，删除 RESTRICT；`(team_id, agent_id) → (agents.team_id, agents.agent_id)`，删除 RESTRICT。
- 显式索引：无；不含 PK/UK 自动索引。

#### 13.2.12 `workflow_versions`：不可变 SOP 定义

definition_json 发布前校验有限 DAG、变量来源、条件、输出 Schema 和失败策略；一个 Run 固定版本及 hash。

| 字段 | 类型 | NULL | 数据库默认 | 含义 |
| --- | --- | --- | --- | --- |
| `team_id` | `VARCHAR(36)` | 否 | — | 所属团队 UUID；租户隔离边界，不由模型或工具输出改写。 |
| `workflow_version_id` | `VARCHAR(36)` | 否 | — | 不可变 SOP 发布版本 UUID。 |
| `agent_id` | `VARCHAR(36)` | 否 | — | 所属 Agent UUID；关联须同时校验团队和资源作用域。 |
| `name` | `VARCHAR(128)` | 否 | — | 资源名称；唯一性只以本表实际 UK 为准。 |
| `version` | `BIGINT` | 否 | — | 资源内不可变内容发布序号；不与 CAS revision 混用。 |
| `definition_json` | `TEXT` | 否 | — | 不可变 WorkflowDefinition JSON，包含有限 DAG 步骤，见 13.8。 |
| `hash` | `VARCHAR(64)` | 否 | — | 规范化版本内容的 SHA-256，小写十六进制；使用时核对内容。 |
| `created_at` | `BIGINT` | 否 | — | 创建 UTC 毫秒时间；服务显式填写 StorageClock，不把历史默认 0 当有效时间。 |
| `updated_at` | `BIGINT` | 否 | — | 最后持久变更 UTC 毫秒时间；由服务显式更新，无自动更新时间触发器。 |

- PK：`(workflow_version_id)`。
- UK：`(team_id, workflow_version_id)`；`(team_id, agent_id, name, version)`。
- FK：`(team_id, agent_id) → (agents.team_id, agents.agent_id)`，删除 RESTRICT。
- 显式索引：无；不含 PK/UK 自动索引。

#### 13.2.13 `threads`：业务会话

active_run_id 约束同 Thread 逻辑串行位置；waiting 保留位置但释放执行租约。summary_ref 为预留，当前主图未完整消费历史摘要。

| 字段 | 类型 | NULL | 数据库默认 | 含义 |
| --- | --- | --- | --- | --- |
| `team_id` | `VARCHAR(36)` | 否 | — | 所属团队 UUID；租户隔离边界，不由模型或工具输出改写。 |
| `thread_id` | `VARCHAR(36)` | 否 | — | 业务会话 UUID；图执行的 configurable.thread_id 则固定使用 run_id。 |
| `agent_id` | `VARCHAR(36)` | 否 | — | 所属 Agent UUID；关联须同时校验团队和资源作用域。 |
| `active_run_id` | `VARCHAR(36)` | 是 | — | 同 Thread 的逻辑活动 Run；waiting 仍占此位置，queued 不抢占。 |
| `summary_ref` | `TEXT` | 是 | — | 会话摘要预留引用；字段存在不代表多轮摘要已接入。 |
| `revision` | `BIGINT` | 否 | `0` | 乐观并发版本，CAS 成功后递增；不等于内容发布序号。 |
| `created_at` | `BIGINT` | 否 | `0` | 创建 UTC 毫秒时间；服务显式填写 StorageClock，不把历史默认 0 当有效时间。 |
| `updated_at` | `BIGINT` | 否 | `0` | 最后持久变更 UTC 毫秒时间；由服务显式更新，无自动更新时间触发器。 |

- PK：`(thread_id)`。
- UK：`(team_id, thread_id)`；`(team_id, thread_id, agent_id)`。
- FK：`(team_id) → (teams.team_id)`，删除 RESTRICT；`(team_id, thread_id, active_run_id) → (runs.team_id, runs.thread_id, runs.run_id)`，删除 RESTRICT；`(team_id, agent_id) → (agents.team_id, agents.agent_id)`，删除 RESTRICT。
- 显式索引：无；不含 PK/UK 自动索引。

#### 13.2.14 `messages`：会话消息

role 为 user/assistant 等消息角色；content_ref 当前直接保存 JSON。source_id 可空，非空时团队内唯一；服务还须核对 run_id 与 thread_id 同属。

| 字段 | 类型 | NULL | 数据库默认 | 含义 |
| --- | --- | --- | --- | --- |
| `team_id` | `VARCHAR(36)` | 否 | — | 所属团队 UUID；租户隔离边界，不由模型或工具输出改写。 |
| `message_id` | `VARCHAR(36)` | 否 | — | 关联消息标识；run_events 可为尚未落成 messages 行的草稿标识。 |
| `thread_id` | `VARCHAR(36)` | 否 | — | 业务会话 UUID；图执行的 configurable.thread_id 则固定使用 run_id。 |
| `run_id` | `VARCHAR(36)` | 是 | — | 关联 Run UUID；所有读写同时限定 team_id 并检查业务归属。 |
| `role` | `VARCHAR(32)` | 否 | — | 团队角色或消息角色，允许值见本表说明。 |
| `content_ref` | `TEXT` | 否 | — | 当前消息输入请求或 outcome 包装 JSON；不是必然为外部文件地址。 |
| `source_id` | `VARCHAR(128)` | 是 | — | 来源消息去重键；非空时在团队内唯一。 |
| `created_at` | `BIGINT` | 否 | `0` | 创建 UTC 毫秒时间；服务显式填写 StorageClock，不把历史默认 0 当有效时间。 |
| `updated_at` | `BIGINT` | 否 | `0` | 最后持久变更 UTC 毫秒时间；由服务显式更新，无自动更新时间触发器。 |

- PK：`(message_id)`。
- UK：`(team_id, message_id)`；`(team_id, source_id)`。
- FK：`(team_id) → (teams.team_id)`，删除 RESTRICT；`(team_id, run_id) → (runs.team_id, runs.run_id)`，删除 RESTRICT；`(team_id, thread_id) → (threads.team_id, threads.thread_id)`，删除 RESTRICT。
- 显式索引：`ix_messages_thread_time(thread_id, created_at)`。

#### 13.2.15 `runs`：运行实例与状态

versions/budgets 接受后固定；状态转换见第 14 节。run_epoch 是所有权代数，revision 是公开 CAS 版本。活跃预算、总存活和等待期限独立，终态不回退。

| 字段 | 类型 | NULL | 数据库默认 | 含义 |
| --- | --- | --- | --- | --- |
| `team_id` | `VARCHAR(36)` | 否 | — | 所属团队 UUID；租户隔离边界，不由模型或工具输出改写。 |
| `run_id` | `VARCHAR(36)` | 否 | — | 关联 Run UUID；所有读写同时限定 team_id 并检查业务归属。 |
| `thread_id` | `VARCHAR(36)` | 否 | — | 业务会话 UUID；图执行的 configurable.thread_id 则固定使用 run_id。 |
| `agent_id` | `VARCHAR(36)` | 否 | — | 所属 Agent UUID；关联须同时校验团队和资源作用域。 |
| `status` | `VARCHAR(32)` | 否 | — | 本表领域状态，具体语义见表说明及第 14 节；数据库不是原生 Enum。 |
| `phase` | `VARCHAR(32)` | 是 | — | 当前执行阶段，不是按秒数推算的完成百分比。 |
| `runtime_profile_id` | `VARCHAR(36)` | 否 | — | 接受 Run 时固定的 runtime_profiles.profile_id。 |
| `versions` | `TEXT` | 否 | — | 完整冻结 RunContext JSON，包括身份、版本和工作流引用，见 13.8。 |
| `budgets` | `TEXT` | 否 | — | 接受时固定的最终预算 JSON，见 13.8。 |
| `run_epoch` | `BIGINT` | 否 | `0` | Run 执行所有权代数，接管后单调递增以拒绝旧执行写入。 |
| `revision` | `BIGINT` | 否 | `0` | 乐观并发版本，CAS 成功后递增；不等于内容发布序号。 |
| `last_event_seq` | `BIGINT` | 否 | `0` | 该 Run/管理任务已分配的最高持久事件序号。 |
| `deadline_at` | `BIGINT` | 是 | — | 当前活跃执行片段的预算截止 UTC 毫秒时间；不等于总存活期限。 |
| `wall_deadline_at` | `BIGINT` | 否 | — | Run 总存活硬截止 UTC 毫秒时间，等待和恢复不刷新。 |
| `waiting_deadline_at` | `BIGINT` | 是 | — | 当前等待输入/审批截止 UTC 毫秒时间；非等待可空。 |
| `outcome_ref` | `TEXT` | 是 | — | run_outcomes.result_id 的逻辑引用，当前无物理 FK。 |
| `recovery_phase` | `VARCHAR(32)` | 是 | — | 持久恢复意图，如 projection/finalizing。 |
| `error_code` | `VARCHAR(128)` | 是 | — | 固定可公开错误代码，不存原始敏感异常。 |
| `created_at` | `BIGINT` | 否 | `0` | 创建 UTC 毫秒时间；服务显式填写 StorageClock，不把历史默认 0 当有效时间。 |
| `updated_at` | `BIGINT` | 否 | `0` | 最后持久变更 UTC 毫秒时间；由服务显式更新，无自动更新时间触发器。 |
| `started_at` | `BIGINT` | 是 | — | 首次实际开始执行的 UTC 毫秒时间；未开始为空。 |
| `active_elapsed_ms` | `BIGINT` | 否 | `0` | 已关闭的活跃执行片段累计毫秒，不包含等待用户的时间。 |
| `active_since` | `BIGINT` | 是 | — | 当前活跃执行片段开始的 UTC 毫秒时间，无活跃片段时为空。 |
| `event_floor` | `BIGINT` | 否 | `0` | 已清理/过期事件的持久高水位，不随物理删除回退。 |

- PK：`(run_id)`。
- UK：`(team_id, run_id)`；`(team_id, thread_id, run_id)`。
- FK：`(team_id) → (teams.team_id)`，删除 RESTRICT；`(team_id, runtime_profile_id) → (runtime_profiles.team_id, runtime_profiles.profile_id)`，删除 RESTRICT；`(team_id, thread_id, agent_id) → (threads.team_id, threads.thread_id, threads.agent_id)`，删除 RESTRICT。
- 显式索引：`ix_runs_status_created(status, created_at)`。

#### 13.2.16 `run_jobs`：持久执行 Job

同 Run/generation/kind 唯一。调度状态为 ready/retry_wait/capability_blocked，领取为 leased，结束为 done/dead；不要与 Run 的 queued 混用。capabilities 保存所需 Graph/Codec 等能力 JSON。

| 字段 | 类型 | NULL | 数据库默认 | 含义 |
| --- | --- | --- | --- | --- |
| `team_id` | `VARCHAR(36)` | 否 | — | 所属团队 UUID；租户隔离边界，不由模型或工具输出改写。 |
| `job_id` | `VARCHAR(36)` | 否 | — | 本表所属或关联的 Job UUID；管理任务与 Run Job 使用不同表。 |
| `run_id` | `VARCHAR(36)` | 否 | — | 关联 Run UUID；所有读写同时限定 team_id 并检查业务归属。 |
| `generation` | `BIGINT` | 否 | — | 同 Run 的执行 Job 代数，与 attempt 和 run_epoch 分别解释。 |
| `kind` | `VARCHAR(32)` | 否 | — | 类型分辨字段，按本表对应领域 DTO 校验。 |
| `status` | `VARCHAR(32)` | 否 | — | 本表领域状态，具体语义见表说明及第 14 节；数据库不是原生 Enum。 |
| `available_at` | `BIGINT` | 否 | — | 允许领取或重试的最早 UTC 毫秒时间。 |
| `priority` | `BIGINT` | 否 | `0` | 调度优先级，结合团队公平轮转解释。 |
| `lease_owner` | `VARCHAR(128)` | 是 | — | 当前领取者身份，非持有期为空。 |
| `lease_until` | `BIGINT` | 是 | — | 领取到期 UTC 毫秒时间；与所有者和代数同时检查。 |
| `attempt` | `BIGINT` | 否 | `0` | 实际执行/发送尝试计数；纯查询、幂等重放和投影修复不能凭空增加。 |
| `capabilities` | `TEXT` | 否 | — | 版本化能力/执行要求 JSON；具体内容见本表说明。 |
| `revision` | `BIGINT` | 否 | `0` | 乐观并发版本，CAS 成功后递增；不等于内容发布序号。 |
| `created_at` | `BIGINT` | 否 | `0` | 创建 UTC 毫秒时间；服务显式填写 StorageClock，不把历史默认 0 当有效时间。 |
| `updated_at` | `BIGINT` | 否 | `0` | 最后持久变更 UTC 毫秒时间；由服务显式更新，无自动更新时间触发器。 |

- PK：`(job_id)`。
- UK：`(run_id, generation, kind)`；`(team_id, job_id)`；`(team_id, run_id, job_id)`。
- FK：`(team_id) → (teams.team_id)`，删除 RESTRICT；`(team_id, run_id) → (runs.team_id, runs.run_id)`，删除 RESTRICT。
- 显式索引：`ix_claimable_jobs(status, available_at, priority, created_at)`。

#### 13.2.17 `execution_leases`：Thread 执行租约

thread_id 为 PK，每 Thread 一条执行租约。Guard 同事务核对 team/Thread/Run/Job/Worker/epoch/有效期及允许状态；guard_revision 为租约内部 CAS 代数。

| 字段 | 类型 | NULL | 数据库默认 | 含义 |
| --- | --- | --- | --- | --- |
| `team_id` | `VARCHAR(36)` | 否 | — | 所属团队 UUID；租户隔离边界，不由模型或工具输出改写。 |
| `thread_id` | `VARCHAR(36)` | 否 | — | 业务会话 UUID；图执行的 configurable.thread_id 则固定使用 run_id。 |
| `owner_run_id` | `VARCHAR(36)` | 否 | — | 执行租约所绑定的 Run。 |
| `owner_job_id` | `VARCHAR(36)` | 否 | — | 执行租约所绑定的 Run Job。 |
| `worker_id` | `VARCHAR(128)` | 否 | — | 本次执行 Worker 身份，必须与有效租约匹配。 |
| `run_epoch` | `BIGINT` | 否 | — | Run 执行所有权代数，接管后单调递增以拒绝旧执行写入。 |
| `expires_at` | `BIGINT` | 否 | — | 到期 UTC 毫秒时间；到期不代表已经物理删除。 |
| `guard_revision` | `BIGINT` | 否 | `0` | 租约或管理任务领取的内部 CAS 版本，不是公开 Run revision。 |

- PK：`(thread_id)`。
- UK：无额外唯一约束。
- FK：`(team_id, owner_run_id, owner_job_id) → (run_jobs.team_id, run_jobs.run_id, run_jobs.job_id)`，删除 RESTRICT；`(team_id, thread_id) → (threads.team_id, threads.thread_id)`，删除 RESTRICT；`(team_id, thread_id, owner_run_id) → (runs.team_id, runs.thread_id, runs.run_id)`，删除 RESTRICT。
- 显式索引：`ix_expired_leases(expires_at, owner_run_id)`。

#### 13.2.18 `run_events`：持久事件与 SSE

PK(run_id,seq)；事件分配与 Run.last_event_seq 更新同事务，提交后再推送。schema_version 当前字符串 1；message_id 无物理 FK，可关联草稿身份。

| 字段 | 类型 | NULL | 数据库默认 | 含义 |
| --- | --- | --- | --- | --- |
| `team_id` | `VARCHAR(36)` | 否 | — | 所属团队 UUID；租户隔离边界，不由模型或工具输出改写。 |
| `run_id` | `VARCHAR(36)` | 否 | — | 关联 Run UUID；所有读写同时限定 team_id 并检查业务归属。 |
| `seq` | `BIGINT` | 否 | — | 当前 Run 或管理任务内部从 1 开始的单调事件序号。 |
| `type` | `VARCHAR(128)` | 否 | — | 公开版本化事件类型，Run 事件见第 12.2 节。 |
| `attempt` | `BIGINT` | 否 | `0` | 实际执行/发送尝试计数；纯查询、幂等重放和投影修复不能凭空增加。 |
| `payload` | `TEXT` | 否 | — | 版本化正文；具体格式和可信边界见本表说明及 13.8。 |
| `occurred_at` | `BIGINT` | 否 | — | 持久事件所用 UTC 毫秒时间。 |
| `schema_version` | `VARCHAR(16)` | 否 | `'1'` | 版本标记；事件信封为字符串 1，与存储协议 dod-storage-agent-v1 分开。 |
| `message_id` | `VARCHAR(36)` | 是 | — | 关联消息标识；run_events 可为尚未落成 messages 行的草稿标识。 |

- PK：`(run_id, seq)`。
- UK：无额外唯一约束。
- FK：`(team_id, run_id) → (runs.team_id, runs.run_id)`，删除 RESTRICT。
- 显式索引：`ix_run_events_time(occurred_at)`。

#### 13.2.19 `interrupts`：具名中断等待点

kind 为 user_input/approval 等，schema 为含 name/kind/actor/响应 Schema 的描述。accepted 指针是唯一有效恢复请求权威，不能用条件唯一索引替代 CAS。

| 字段 | 类型 | NULL | 数据库默认 | 含义 |
| --- | --- | --- | --- | --- |
| `team_id` | `VARCHAR(36)` | 否 | — | 所属团队 UUID；租户隔离边界，不由模型或工具输出改写。 |
| `interrupt_id` | `VARCHAR(36)` | 否 | — | 公开具名等待点 UUID。 |
| `run_id` | `VARCHAR(36)` | 否 | — | 关联 Run UUID；所有读写同时限定 team_id 并检查业务归属。 |
| `graph_interrupt_id` | `VARCHAR(128)` | 否 | — | 框架中断标识；同 Run 内唯一。 |
| `kind` | `VARCHAR(32)` | 否 | — | 类型分辨字段，按本表对应领域 DTO 校验。 |
| `status` | `VARCHAR(32)` | 否 | — | 本表领域状态，具体语义见表说明及第 14 节；数据库不是原生 Enum。 |
| `schema` | `TEXT` | 否 | — | 工具完整配置或中断描述 JSON，不是必然只有输入 JSON Schema。 |
| `expires_at` | `BIGINT` | 否 | — | 到期 UTC 毫秒时间；到期不代表已经物理删除。 |
| `args_hash` | `VARCHAR(64)` | 是 | — | 规范化参数摘要；审批表中严格等于 Action.payload_hash。 |
| `accepted_resume_request_id` | `VARCHAR(36)` | 是 | — | 唯一有效恢复请求指针，指向同 interrupt 的 request_id；仅通过 CAS 从 NULL 赋值。 |
| `revision` | `BIGINT` | 否 | `0` | 乐观并发版本，CAS 成功后递增；不等于内容发布序号。 |
| `created_at` | `BIGINT` | 否 | `0` | 创建 UTC 毫秒时间；服务显式填写 StorageClock，不把历史默认 0 当有效时间。 |
| `updated_at` | `BIGINT` | 否 | `0` | 最后持久变更 UTC 毫秒时间；由服务显式更新，无自动更新时间触发器。 |

- PK：`(interrupt_id)`。
- UK：`(run_id, graph_interrupt_id)`；`(team_id, interrupt_id)`；`(team_id, run_id, interrupt_id)`。
- FK：`(team_id) → (teams.team_id)`，删除 RESTRICT；`(team_id, interrupt_id, accepted_resume_request_id) → (resume_requests.team_id, resume_requests.interrupt_id, resume_requests.request_id)`，删除 RESTRICT；`(team_id, run_id) → (runs.team_id, runs.run_id)`，删除 RESTRICT。
- 显式索引：无；不含 PK/UK 自动索引。

#### 13.2.20 `resume_requests`：持久恢复输入

response 为类型化 user_input/approval；status 经 submitted→applying→applied。图内 last_applied_resume_id 是 Command 已应用的持久证据，不能因投影滞后重复投递。

| 字段 | 类型 | NULL | 数据库默认 | 含义 |
| --- | --- | --- | --- | --- |
| `team_id` | `VARCHAR(36)` | 否 | — | 所属团队 UUID；租户隔离边界，不由模型或工具输出改写。 |
| `request_id` | `VARCHAR(36)` | 否 | — | 恢复输入提交 UUID。 |
| `run_id` | `VARCHAR(36)` | 否 | — | 关联 Run UUID；所有读写同时限定 team_id 并检查业务归属。 |
| `interrupt_id` | `VARCHAR(36)` | 否 | — | 公开具名等待点 UUID。 |
| `response` | `TEXT` | 否 | — | 类型化恢复输入或最终返回视图 JSON，见本表说明。 |
| `status` | `VARCHAR(32)` | 否 | — | 本表领域状态，具体语义见表说明及第 14 节；数据库不是原生 Enum。 |
| `idempotency_key` | `VARCHAR(128)` | 否 | — | 调用方幂等键，仅在本表规定作用域内解释。 |
| `created_at` | `BIGINT` | 否 | `0` | 创建 UTC 毫秒时间；服务显式填写 StorageClock，不把历史默认 0 当有效时间。 |
| `updated_at` | `BIGINT` | 否 | `0` | 最后持久变更 UTC 毫秒时间；由服务显式更新，无自动更新时间触发器。 |

- PK：`(request_id)`。
- UK：`(interrupt_id, idempotency_key)`；`(team_id, interrupt_id, request_id)`；`(team_id, request_id)`。
- FK：`(team_id) → (teams.team_id)`，删除 RESTRICT；`(team_id, run_id, interrupt_id) → (interrupts.team_id, interrupts.run_id, interrupts.interrupt_id)`，删除 RESTRICT。
- 显式索引：无；不含 PK/UK 自动索引。

#### 13.2.21 `query_scope_revisions`：查询范围修订

同 Run version 递增，当前范围按最大版本解析；Scope 修改新增行，历史不改写。旧范围证据不能冒充新范围的正常结论。

| 字段 | 类型 | NULL | 数据库默认 | 含义 |
| --- | --- | --- | --- | --- |
| `team_id` | `VARCHAR(36)` | 否 | — | 所属团队 UUID；租户隔离边界，不由模型或工具输出改写。 |
| `scope_id` | `VARCHAR(36)` | 否 | — | QueryScope 修订 UUID；是否存在物理 FK 以本表 FK 列表为准。 |
| `run_id` | `VARCHAR(36)` | 否 | — | 关联 Run UUID；所有读写同时限定 team_id 并检查业务归属。 |
| `version` | `BIGINT` | 否 | — | 资源内不可变内容发布序号；不与 CAS revision 混用。 |
| `scope_json` | `TEXT` | 否 | — | 已解析查询范围 JSON，包括服务、环境、区域和带时区时间窗。 |
| `hash` | `VARCHAR(64)` | 否 | — | 规范化版本内容的 SHA-256，小写十六进制；使用时核对内容。 |
| `created_by` | `VARCHAR(128)` | 否 | — | 创建记录的可信用户/服务主体。 |
| `created_at` | `BIGINT` | 否 | `0` | 创建 UTC 毫秒时间；服务显式填写 StorageClock，不把历史默认 0 当有效时间。 |
| `updated_at` | `BIGINT` | 否 | `0` | 最后持久变更 UTC 毫秒时间；由服务显式更新，无自动更新时间触发器。 |

- PK：`(scope_id)`。
- UK：`(run_id, version)`；`(team_id, scope_id)`。
- FK：`(team_id) → (teams.team_id)`，删除 RESTRICT；`(team_id, run_id) → (runs.team_id, runs.run_id)`，删除 RESTRICT。
- 显式索引：无；不含 PK/UK 自动索引。

#### 13.2.22 `run_outcomes`：最终结构化结果

run_id 是 PK，每 Run 一份最终结果；重放必须同 digest。response 是最终 RunView JSON 快照，读取仍需重验当前授权。payload 为 RunOutcome JSON。

| 字段 | 类型 | NULL | 数据库默认 | 含义 |
| --- | --- | --- | --- | --- |
| `team_id` | `VARCHAR(36)` | 否 | — | 所属团队 UUID；租户隔离边界，不由模型或工具输出改写。 |
| `run_id` | `VARCHAR(36)` | 否 | — | 关联 Run UUID；所有读写同时限定 team_id 并检查业务归属。 |
| `result_id` | `VARCHAR(36)` | 否 | — | 某 Run 最终结果 UUID，当前采用确定性身份。 |
| `digest` | `VARCHAR(64)` | 否 | — | 规范化 RunOutcome JSON 的 SHA-256。 |
| `payload` | `TEXT` | 否 | — | 版本化正文；具体格式和可信边界见本表说明及 13.8。 |
| `message_id` | `VARCHAR(36)` | 否 | — | 关联消息标识；run_events 可为尚未落成 messages 行的草稿标识。 |
| `response` | `TEXT` | 是 | — | 类型化恢复输入或最终返回视图 JSON，见本表说明。 |
| `created_at` | `BIGINT` | 否 | — | 创建 UTC 毫秒时间；服务显式填写 StorageClock，不把历史默认 0 当有效时间。 |

- PK：`(run_id)`。
- UK：`(team_id, result_id)`。
- FK：`(team_id, message_id) → (messages.team_id, messages.message_id)`，删除 RESTRICT；`(team_id, run_id) → (runs.team_id, runs.run_id)`，删除 RESTRICT。
- 显式索引：无；不含 PK/UK 自动索引。

#### 13.2.23 `tool_invocations`：工具与 Runner 调用台账

tool_version 当前通常保存绑定 resource_id，Runner 路径可保存快照 ID；完整版本/hash/Scope 在回执核对。派发前保存 possibly_sent，未知结果不得自动重发。

| 字段 | 类型 | NULL | 数据库默认 | 含义 |
| --- | --- | --- | --- | --- |
| `team_id` | `VARCHAR(36)` | 否 | — | 所属团队 UUID；租户隔离边界，不由模型或工具输出改写。 |
| `invocation_id` | `VARCHAR(36)` | 否 | — | 一次工具/Runner 执行尝试 UUID。 |
| `run_id` | `VARCHAR(36)` | 否 | — | 关联 Run UUID；所有读写同时限定 team_id 并检查业务归属。 |
| `logical_call_id` | `VARCHAR(128)` | 否 | — | 跨恢复稳定的逻辑工具调用标识。 |
| `tool_version` | `VARCHAR(128)` | 否 | — | 工具版本标识；调用台账与 Action 的实际编码不同，见本表说明。 |
| `args_hash` | `VARCHAR(64)` | 否 | — | 规范化参数摘要；审批表中严格等于 Action.payload_hash。 |
| `attempt` | `BIGINT` | 否 | — | 实际执行/发送尝试计数；纯查询、幂等重放和投影修复不能凭空增加。 |
| `status` | `VARCHAR(32)` | 否 | — | 本表领域状态，具体语义见表说明及第 14 节；数据库不是原生 Enum。 |
| `receipt_ref` | `TEXT` | 是 | — | 工具/Runner/Outbox 的可信回执 JSON，格式见本表说明及 13.8。 |
| `created_at` | `BIGINT` | 否 | `0` | 创建 UTC 毫秒时间；服务显式填写 StorageClock，不把历史默认 0 当有效时间。 |
| `updated_at` | `BIGINT` | 否 | `0` | 最后持久变更 UTC 毫秒时间；由服务显式更新，无自动更新时间触发器。 |

- PK：`(invocation_id)`。
- UK：`(run_id, logical_call_id, attempt)`；`(team_id, invocation_id)`。
- FK：`(team_id) → (teams.team_id)`，删除 RESTRICT；`(team_id, run_id) → (runs.team_id, runs.run_id)`，删除 RESTRICT。
- 显式索引：无；不含 PK/UK 自动索引。

#### 13.2.24 `evidence`：证据元数据

物理 FK 只分别保证 Run/Scope 同团队，服务还要检查同 Run。quality 对应领域 quality_status；summary 可能只是 digest，大正文使用 Artifact/正文表。

| 字段 | 类型 | NULL | 数据库默认 | 含义 |
| --- | --- | --- | --- | --- |
| `team_id` | `VARCHAR(36)` | 否 | — | 所属团队 UUID；租户隔离边界，不由模型或工具输出改写。 |
| `evidence_id` | `VARCHAR(36)` | 否 | — | 证据 UUID；正文表与元数据表一对一。 |
| `run_id` | `VARCHAR(36)` | 否 | — | 关联 Run UUID；所有读写同时限定 team_id 并检查业务归属。 |
| `scope_id` | `VARCHAR(36)` | 否 | — | QueryScope 修订 UUID；是否存在物理 FK 以本表 FK 列表为准。 |
| `source` | `VARCHAR(128)` | 否 | — | 证据/事件来源键，按来源协议解释。 |
| `quality` | `VARCHAR(32)` | 否 | — | 证据质量：valid/empty/unavailable/conflicting/stale 等；当前也使用 truncated。 |
| `time_window` | `TEXT` | 否 | — | 证据实际查询窗 JSON；仓库可为 {}，值班可记录 observed_at_ms。 |
| `artifact_ref` | `VARCHAR(36)` | 是 | — | 关联 artifacts.artifact_id；大正文或文件的受控引用。 |
| `summary` | `TEXT` | 是 | — | 证据简述/摘要或知识问题描述；不保证是完整可读原文。 |
| `created_at` | `BIGINT` | 否 | `0` | 创建 UTC 毫秒时间；服务显式填写 StorageClock，不把历史默认 0 当有效时间。 |
| `updated_at` | `BIGINT` | 否 | `0` | 最后持久变更 UTC 毫秒时间；由服务显式更新，无自动更新时间触发器。 |

- PK：`(evidence_id)`。
- UK：`(team_id, evidence_id)`。
- FK：`(team_id) → (teams.team_id)`，删除 RESTRICT；`(team_id, artifact_ref) → (artifacts.team_id, artifacts.artifact_id)`，删除 RESTRICT；`(team_id, run_id) → (runs.team_id, runs.run_id)`，删除 RESTRICT；`(team_id, scope_id) → (query_scope_revisions.team_id, query_scope_revisions.scope_id)`，删除 RESTRICT。
- 显式索引：`ix_evidence_source(run_id, source)`。

#### 13.2.25 `evidence_payloads`：有界证据正文

每 evidence_id 一份 payload；与 evidence 和 Run 一致性同事务核对。正文为工具协议 JSON 或 commit/path/lines 的仓库证据，truncated=1 不视为完整引用。

| 字段 | 类型 | NULL | 数据库默认 | 含义 |
| --- | --- | --- | --- | --- |
| `team_id` | `VARCHAR(36)` | 否 | — | 所属团队 UUID；租户隔离边界，不由模型或工具输出改写。 |
| `evidence_id` | `VARCHAR(36)` | 否 | — | 证据 UUID；正文表与元数据表一对一。 |
| `run_id` | `VARCHAR(36)` | 否 | — | 关联 Run UUID；所有读写同时限定 team_id 并检查业务归属。 |
| `payload` | `TEXT` | 否 | — | 版本化正文；具体格式和可信边界见本表说明及 13.8。 |
| `truncated` | `BIGINT` | 否 | — | 0/1 整数截断标志；1 不能视为完整可访问的证据正文。 |
| `created_at` | `BIGINT` | 否 | — | 创建 UTC 毫秒时间；服务显式填写 StorageClock，不把历史默认 0 当有效时间。 |

- PK：`(evidence_id)`。
- UK：`(team_id, evidence_id)`。
- FK：`(team_id, evidence_id) → (evidence.team_id, evidence.evidence_id)`，删除 RESTRICT；`(team_id, run_id) → (runs.team_id, runs.run_id)`，删除 RESTRICT。
- 显式索引：无；不含 PK/UK 自动索引。

#### 13.2.26 `artifacts`：对象元数据

先 uploading，写对象并回读校验长度/sha256 后才 available；状态改变还须检查当前授权和 Scope。object_key 不是可公开 URL。

| 字段 | 类型 | NULL | 数据库默认 | 含义 |
| --- | --- | --- | --- | --- |
| `team_id` | `VARCHAR(36)` | 否 | — | 所属团队 UUID；租户隔离边界，不由模型或工具输出改写。 |
| `artifact_id` | `VARCHAR(36)` | 否 | — | 文件产物 UUID。 |
| `run_id` | `VARCHAR(36)` | 是 | — | 关联 Run UUID；所有读写同时限定 team_id 并检查业务归属。 |
| `kind` | `VARCHAR(32)` | 否 | — | 类型分辨字段，按本表对应领域 DTO 校验。 |
| `object_key` | `TEXT` | 否 | — | 对象键；上传期间可以是 pending:<id>，不是下载 URL。 |
| `sha256` | `VARCHAR(64)` | 否 | — | 对象原始文件字节 SHA-256。 |
| `size` | `BIGINT` | 否 | — | 对象字节数；CHECK 禁止负数，大小上限另由服务校验。 |
| `status` | `VARCHAR(32)` | 否 | — | 本表领域状态，具体语义见表说明及第 14 节；数据库不是原生 Enum。 |
| `expires_at` | `BIGINT` | 是 | — | 到期 UTC 毫秒时间；到期不代表已经物理删除。 |
| `created_at` | `BIGINT` | 否 | `0` | 创建 UTC 毫秒时间；服务显式填写 StorageClock，不把历史默认 0 当有效时间。 |
| `updated_at` | `BIGINT` | 否 | `0` | 最后持久变更 UTC 毫秒时间；由服务显式更新，无自动更新时间触发器。 |

- PK：`(artifact_id)`。
- UK：`(team_id, artifact_id)`。
- FK：`(team_id) → (teams.team_id)`，删除 RESTRICT；`(team_id, run_id) → (runs.team_id, runs.run_id)`，删除 RESTRICT。
- 显式索引：无；不含 PK/UK 自动索引。
- CHECK：`size >= 0`。

#### 13.2.27 `artifact_upload_scopes`：上传范围绑定

artifact_id 与 Artifact 一对一；scope_id 当前无物理 FK。受理、完成、下载均重验同 Run 当前 Scope；无 Scope 时允许 NULL。

| 字段 | 类型 | NULL | 数据库默认 | 含义 |
| --- | --- | --- | --- | --- |
| `team_id` | `VARCHAR(36)` | 否 | — | 所属团队 UUID；租户隔离边界，不由模型或工具输出改写。 |
| `artifact_id` | `VARCHAR(36)` | 否 | — | 文件产物 UUID。 |
| `run_id` | `VARCHAR(36)` | 否 | — | 关联 Run UUID；所有读写同时限定 team_id 并检查业务归属。 |
| `scope_id` | `VARCHAR(36)` | 是 | — | QueryScope 修订 UUID；是否存在物理 FK 以本表 FK 列表为准。 |
| `created_at` | `BIGINT` | 否 | — | 创建 UTC 毫秒时间；服务显式填写 StorageClock，不把历史默认 0 当有效时间。 |

- PK：`(artifact_id)`。
- UK：`(team_id, artifact_id)`。
- FK：`(team_id, artifact_id) → (artifacts.team_id, artifacts.artifact_id)`，删除 RESTRICT；`(team_id, run_id) → (runs.team_id, runs.run_id)`，删除 RESTRICT。
- 显式索引：无；不含 PK/UK 自动索引。

#### 13.2.28 `runner_cleanup_obligations`：Runner 独立清理义务

创建资源前提交；status=pending/cleaning/cleaned。维护领取不使用过期 Run Guard，但必须确认原执行失活及 daemon 一致；派发未确认不能因暂时查无资源就清除义务。

| 字段 | 类型 | NULL | 数据库默认 | 含义 |
| --- | --- | --- | --- | --- |
| `team_id` | `VARCHAR(36)` | 否 | — | 所属团队 UUID；租户隔离边界，不由模型或工具输出改写。 |
| `invocation_id` | `VARCHAR(36)` | 否 | — | 一次工具/Runner 执行尝试 UUID。 |
| `run_id` | `VARCHAR(36)` | 否 | — | 关联 Run UUID；所有读写同时限定 team_id 并检查业务归属。 |
| `thread_id` | `VARCHAR(36)` | 否 | — | 业务会话 UUID；图执行的 configurable.thread_id 则固定使用 run_id。 |
| `job_id` | `VARCHAR(36)` | 否 | — | 本表所属或关联的 Job UUID；管理任务与 Run Job 使用不同表。 |
| `worker_id` | `VARCHAR(128)` | 否 | — | 本次执行 Worker 身份，必须与有效租约匹配。 |
| `run_epoch` | `BIGINT` | 否 | — | Run 执行所有权代数，接管后单调递增以拒绝旧执行写入。 |
| `daemon_id` | `VARCHAR(128)` | 否 | — | 受信 Docker Engine ID 或显式预置的 Podman 身份。 |
| `container_name` | `VARCHAR(64)` | 否 | — | 本次容器资源名，全表唯一，格式 dod-run-<32位十六进制>。 |
| `volume_name` | `VARCHAR(64)` | 否 | — | Runner 本次任务卷，全表唯一，格式 dod-task-<32位十六进制>。 |
| `status` | `VARCHAR(24)` | 否 | — | 本表领域状态，具体语义见表说明及第 14 节；数据库不是原生 Enum。 |
| `cleanup_owner` | `VARCHAR(36)` | 是 | — | 独立维护领取的随机所有者 UUID。 |
| `cleanup_until` | `BIGINT` | 是 | — | 独立维护领取的到期 UTC 毫秒时间。 |
| `attempts` | `BIGINT` | 否 | — | Runner 独立资源清理尝试次数。 |
| `next_retry_at` | `BIGINT` | 否 | — | 下次允许清理的 UTC 毫秒时间。 |
| `creation_steps` | `BIGINT` | 否 | — | 创建阶段位掩码：0→1 卷派发→3 卷确认→7 容器派发→15 容器确认。 |
| `created_at` | `BIGINT` | 否 | — | 创建 UTC 毫秒时间；服务显式填写 StorageClock，不把历史默认 0 当有效时间。 |
| `updated_at` | `BIGINT` | 否 | — | 最后持久变更 UTC 毫秒时间；由服务显式更新，无自动更新时间触发器。 |

- PK：`(invocation_id)`。
- UK：`(container_name)`；`(team_id, invocation_id)`；`(volume_name)`。
- FK：`(team_id, invocation_id) → (tool_invocations.team_id, tool_invocations.invocation_id)`，删除 RESTRICT；`(team_id, run_id) → (runs.team_id, runs.run_id)`，删除 RESTRICT。
- 显式索引：无；不含 PK/UK 自动索引。

#### 13.2.29 `usage_budget_policies`：固定预算解释

每 Run 一行，接受时固定 summary_tokens；恢复不能重新降低预留。遗留策略无法证明时拒绝自动恢复，不猜值补零。

| 字段 | 类型 | NULL | 数据库默认 | 含义 |
| --- | --- | --- | --- | --- |
| `team_id` | `VARCHAR(36)` | 否 | — | 所属团队 UUID；租户隔离边界，不由模型或工具输出改写。 |
| `run_id` | `VARCHAR(36)` | 否 | — | 关联 Run UUID；所有读写同时限定 team_id 并检查业务归属。 |
| `summary_tokens` | `BIGINT` | 否 | — | 接受时固定的最终汇总 Token 预留上界，不是已消费量。 |
| `created_at` | `BIGINT` | 否 | — | 创建 UTC 毫秒时间；服务显式填写 StorageClock，不把历史默认 0 当有效时间。 |

- PK：`(run_id)`。
- UK：无额外唯一约束。
- FK：`(team_id, run_id) → (runs.team_id, runs.run_id)`，删除 RESTRICT。
- 显式索引：无；不含 PK/UK 自动索引。

#### 13.2.30 `usage_reservations`：实际调用额度预留

新记录 kind 为 model/expert/evaluation/summary/tool，legacy 仅兼容历史。request_hash/guard_json 对新记录必填，model_attempts+tool_attempts=1 为应用约束。未知用量继续保守占额。

| 字段 | 类型 | NULL | 数据库默认 | 含义 |
| --- | --- | --- | --- | --- |
| `team_id` | `VARCHAR(36)` | 否 | — | 所属团队 UUID；租户隔离边界，不由模型或工具输出改写。 |
| `reservation_id` | `VARCHAR(36)` | 否 | — | 本次实际调用的预算预留 UUID。 |
| `run_id` | `VARCHAR(36)` | 否 | — | 关联 Run UUID；所有读写同时限定 team_id 并检查业务归属。 |
| `invocation` | `VARCHAR(128)` | 否 | — | 一次实际调用尝试的稳定标识；网络重试使用新的尝试标识。 |
| `tokens` | `BIGINT` | 否 | — | 一次实际尝试预留的 Token 上界；工具通常为 0。 |
| `amount_micros` | `BIGINT` | 是 | — | 金额微单位整数；未知为 NULL，禁止以 0 代替未知。 |
| `currency` | `VARCHAR(3)` | 是 | — | 三位大写币种；已知金额必须同时有币种和计价版本。 |
| `pricing_version` | `VARCHAR(128)` | 否 | — | 计价版本或明确未知标识；恢复不能用当前价格覆盖历史。 |
| `status` | `VARCHAR(32)` | 否 | — | 本表领域状态，具体语义见表说明及第 14 节；数据库不是原生 Enum。 |
| `created_at` | `BIGINT` | 否 | `0` | 创建 UTC 毫秒时间；服务显式填写 StorageClock，不把历史默认 0 当有效时间。 |
| `updated_at` | `BIGINT` | 否 | `0` | 最后持久变更 UTC 毫秒时间；由服务显式更新，无自动更新时间触发器。 |
| `kind` | `VARCHAR(32)` | 否 | `'legacy'` | 类型分辨字段，按本表对应领域 DTO 校验。 |
| `request_hash` | `VARCHAR(64)` | 是 | — | 规范化请求 SHA-256；同一幂等身份出现不同摘要必须冲突。 |
| `guard_json` | `TEXT` | 是 | — | 预留/回执产生时完整 ExecutionGuard 的规范化 JSON。 |
| `scope_id` | `VARCHAR(36)` | 是 | — | QueryScope 修订 UUID；是否存在物理 FK 以本表 FK 列表为准。 |
| `model_attempts` | `BIGINT` | 否 | `0` | 本次预留的实际模型尝试数，新记录通常为 0 或 1。 |
| `tool_attempts` | `BIGINT` | 否 | `0` | 本次预留的实际工具尝试数，新记录通常为 0 或 1。 |
| `send_state` | `VARCHAR(32)` | 否 | `'unsent'` | unsent/possibly_sent/sent；网络派发前持久化 possibly_sent。 |

- PK：`(reservation_id)`。
- UK：`(run_id, invocation)`；`(team_id, reservation_id)`。
- FK：`(team_id) → (teams.team_id)`，删除 RESTRICT；`(team_id, run_id) → (runs.team_id, runs.run_id)`，删除 RESTRICT。
- 显式索引：无；不含 PK/UK 自动索引。
- CHECK：`tokens >= 0`。

#### 13.2.31 `usage_records`：调用结算

每 Run/invocation 一份结算；usage_pending 和 cost_pending 独立，status 为 pending/settled。金额按币种聚合，未知不能记零；不能用旧 Guard 直接写主账。

| 字段 | 类型 | NULL | 数据库默认 | 含义 |
| --- | --- | --- | --- | --- |
| `team_id` | `VARCHAR(36)` | 否 | — | 所属团队 UUID；租户隔离边界，不由模型或工具输出改写。 |
| `record_id` | `VARCHAR(36)` | 否 | — | 幂等响应或用量结算记录 UUID。 |
| `run_id` | `VARCHAR(36)` | 否 | — | 关联 Run UUID；所有读写同时限定 team_id 并检查业务归属。 |
| `invocation` | `VARCHAR(128)` | 否 | — | 一次实际调用尝试的稳定标识；网络重试使用新的尝试标识。 |
| `reservation_id` | `VARCHAR(36)` | 是 | — | 本次实际调用的预算预留 UUID。 |
| `input_tokens` | `BIGINT` | 是 | — | 实际输入 Token；未收到可信计量时为 NULL。 |
| `output_tokens` | `BIGINT` | 是 | — | 实际输出 Token；未收到可信计量时为 NULL。 |
| `amount_micros` | `BIGINT` | 是 | — | 金额微单位整数；未知为 NULL，禁止以 0 代替未知。 |
| `currency` | `VARCHAR(3)` | 是 | — | 三位大写币种；已知金额必须同时有币种和计价版本。 |
| `pricing_version` | `VARCHAR(128)` | 否 | — | 计价版本或明确未知标识；恢复不能用当前价格覆盖历史。 |
| `status` | `VARCHAR(32)` | 否 | — | 本表领域状态，具体语义见表说明及第 14 节；数据库不是原生 Enum。 |
| `created_at` | `BIGINT` | 否 | `0` | 创建 UTC 毫秒时间；服务显式填写 StorageClock，不把历史默认 0 当有效时间。 |
| `updated_at` | `BIGINT` | 否 | `0` | 最后持久变更 UTC 毫秒时间；由服务显式更新，无自动更新时间触发器。 |
| `usage_pending` | `BIGINT` | 否 | `1` | 0/1 整数标志；1 表示实际 Token 计量仍未知。 |
| `cost_pending` | `BIGINT` | 否 | `1` | 0/1 整数标志；1 表示费用未确认，与 Token 计量独立。 |

- PK：`(record_id)`。
- UK：`(run_id, invocation)`；`(team_id, record_id)`。
- FK：`(team_id) → (teams.team_id)`，删除 RESTRICT；`(team_id, reservation_id) → (usage_reservations.team_id, usage_reservations.reservation_id)`，删除 RESTRICT；`(team_id, run_id) → (runs.team_id, runs.run_id)`，删除 RESTRICT。
- 显式索引：无；不含 PK/UK 自动索引。

#### 13.2.32 `usage_quarantine`：失效执行用量回执

旧执行晚返回回执只写此隔离表；payload 为 ActualUsage，status=quarantined/reconciled，可信对账后才在当前上下文结算。

| 字段 | 类型 | NULL | 数据库默认 | 含义 |
| --- | --- | --- | --- | --- |
| `team_id` | `VARCHAR(36)` | 否 | — | 所属团队 UUID；租户隔离边界，不由模型或工具输出改写。 |
| `quarantine_id` | `VARCHAR(36)` | 否 | — | 失效执行的隔离用量回执 UUID。 |
| `run_id` | `VARCHAR(36)` | 否 | — | 关联 Run UUID；所有读写同时限定 team_id 并检查业务归属。 |
| `invocation` | `VARCHAR(128)` | 否 | — | 一次实际调用尝试的稳定标识；网络重试使用新的尝试标识。 |
| `receipt_hash` | `VARCHAR(64)` | 否 | — | 隔离用量回执摘要，用于去重同一迟到回执。 |
| `payload` | `TEXT` | 否 | — | 版本化正文；具体格式和可信边界见本表说明及 13.8。 |
| `guard_json` | `TEXT` | 否 | — | 预留/回执产生时完整 ExecutionGuard 的规范化 JSON。 |
| `status` | `VARCHAR(32)` | 否 | — | 本表领域状态，具体语义见表说明及第 14 节；数据库不是原生 Enum。 |
| `created_at` | `BIGINT` | 否 | — | 创建 UTC 毫秒时间；服务显式填写 StorageClock，不把历史默认 0 当有效时间。 |

- PK：`(quarantine_id)`。
- UK：`(run_id, invocation, receipt_hash)`。
- FK：`(team_id, run_id) → (runs.team_id, runs.run_id)`，删除 RESTRICT。
- 显式索引：无；不含 PK/UK 自动索引。

#### 13.2.33 `actions`：外部逻辑动作

tool_version 是 bindingID:version:hash 串；scope 实际是 QueryScope UUID 的 TEXT，非范围 JSON。payload_ref 含 binding/target/arguments；state 与投递状态分开。

| 字段 | 类型 | NULL | 数据库默认 | 含义 |
| --- | --- | --- | --- | --- |
| `team_id` | `VARCHAR(36)` | 否 | — | 所属团队 UUID；租户隔离边界，不由模型或工具输出改写。 |
| `action_id` | `VARCHAR(36)` | 否 | — | 逻辑外部动作 UUID；也是提交给下游的稳定幂等身份。 |
| `run_id` | `VARCHAR(36)` | 否 | — | 关联 Run UUID；所有读写同时限定 team_id 并检查业务归属。 |
| `action_key` | `VARCHAR(128)` | 否 | — | 团队内唯一的业务逻辑动作键；不能按执行尝试重新随机生成。 |
| `tool_version` | `VARCHAR(128)` | 否 | — | 工具版本标识；调用台账与 Action 的实际编码不同，见本表说明。 |
| `payload_hash` | `VARCHAR(64)` | 否 | — | 规范化正文或动作参数的 SHA-256；动作算法见 13.8。 |
| `payload_ref` | `TEXT` | 否 | — | 持久正文 JSON，具体格式见本表；不是必然为对象存储引用。 |
| `scope` | `TEXT` | 否 | — | 模型范围/工具范围/动作 Scope，三者物理语义见本表说明。 |
| `state` | `VARCHAR(32)` | 否 | — | 逻辑动作状态，转换见第 14 节。 |
| `receipt` | `TEXT` | 是 | — | 可信回执 JSON；审批决定回执与外部业务发送回执不能混用。 |
| `revision` | `BIGINT` | 否 | `0` | 乐观并发版本，CAS 成功后递增；不等于内容发布序号。 |
| `created_at` | `BIGINT` | 否 | `0` | 创建 UTC 毫秒时间；服务显式填写 StorageClock，不把历史默认 0 当有效时间。 |
| `updated_at` | `BIGINT` | 否 | `0` | 最后持久变更 UTC 毫秒时间；由服务显式更新，无自动更新时间触发器。 |

- PK：`(action_id)`。
- UK：`(team_id, action_id)`；`(team_id, action_key)`。
- FK：`(team_id) → (teams.team_id)`，删除 RESTRICT；`(team_id, run_id) → (runs.team_id, runs.run_id)`，删除 RESTRICT。
- 显式索引：无；不含 PK/UK 自动索引。

#### 13.2.34 `approvals`：人工审批历史

一个 Action 可有多次审批；decision=pending/approve/reject，receipt 为审批审计回执，不是业务成功。版本、目标、args_hash、策略、主体、期限全部绑定。

| 字段 | 类型 | NULL | 数据库默认 | 含义 |
| --- | --- | --- | --- | --- |
| `team_id` | `VARCHAR(36)` | 否 | — | 所属团队 UUID；租户隔离边界，不由模型或工具输出改写。 |
| `approval_id` | `VARCHAR(36)` | 否 | — | 某一次审批 UUID；同一 Action 可保留多次审批历史。 |
| `run_id` | `VARCHAR(36)` | 否 | — | 关联 Run UUID；所有读写同时限定 team_id 并检查业务归属。 |
| `action_id` | `VARCHAR(36)` | 否 | — | 逻辑外部动作 UUID；也是提交给下游的稳定幂等身份。 |
| `tool_version` | `VARCHAR(128)` | 否 | — | 工具版本标识；调用台账与 Action 的实际编码不同，见本表说明。 |
| `target` | `TEXT` | 否 | — | 审批目标 JSON：server/credential_scope/tool_name。 |
| `args_hash` | `VARCHAR(64)` | 否 | — | 规范化参数摘要；审批表中严格等于 Action.payload_hash。 |
| `policy_hash` | `VARCHAR(64)` | 否 | — | 规范化授权策略的 SHA-256，发送/审批时重新核对。 |
| `subject` | `VARCHAR(128)` | 否 | — | 身份系统主体字符串，不要求为 UUID，不存认证 Token。 |
| `expires_at` | `BIGINT` | 否 | — | 到期 UTC 毫秒时间；到期不代表已经物理删除。 |
| `decision` | `VARCHAR(32)` | 否 | — | 审批/审计决定；准确取值和含义见本表说明。 |
| `receipt` | `TEXT` | 是 | — | 可信回执 JSON；审批决定回执与外部业务发送回执不能混用。 |
| `created_at` | `BIGINT` | 否 | `0` | 创建 UTC 毫秒时间；服务显式填写 StorageClock，不把历史默认 0 当有效时间。 |
| `updated_at` | `BIGINT` | 否 | `0` | 最后持久变更 UTC 毫秒时间；由服务显式更新，无自动更新时间触发器。 |

- PK：`(approval_id)`。
- UK：`(team_id, approval_id)`。
- FK：`(team_id) → (teams.team_id)`，删除 RESTRICT；`(team_id, action_id) → (actions.team_id, actions.action_id)`，删除 RESTRICT；`(team_id, run_id) → (runs.team_id, runs.run_id)`，删除 RESTRICT。
- 显式索引：无；不含 PK/UK 自动索引。

#### 13.2.35 `outbox`：持久外部发送队列

payload_ref 应与 Action 一致。先提交 sending 再 I/O；过期 sending 变 unknown，不自动重发。status=pending/sending/succeeded/unknown/dead；当前未声明 action_id 唯一性。

| 字段 | 类型 | NULL | 数据库默认 | 含义 |
| --- | --- | --- | --- | --- |
| `team_id` | `VARCHAR(36)` | 否 | — | 所属团队 UUID；租户隔离边界，不由模型或工具输出改写。 |
| `outbox_id` | `VARCHAR(36)` | 否 | — | 外部动作发送记录 UUID。 |
| `action_id` | `VARCHAR(36)` | 否 | — | 逻辑外部动作 UUID；也是提交给下游的稳定幂等身份。 |
| `destination` | `VARCHAR(128)` | 否 | — | 部署注册的 sender/连接器键，不能是任意用户指定的 URL。 |
| `payload_ref` | `TEXT` | 否 | — | 持久正文 JSON，具体格式见本表；不是必然为对象存储引用。 |
| `status` | `VARCHAR(32)` | 否 | — | 本表领域状态，具体语义见表说明及第 14 节；数据库不是原生 Enum。 |
| `available_at` | `BIGINT` | 否 | — | 允许领取或重试的最早 UTC 毫秒时间。 |
| `attempt` | `BIGINT` | 否 | `0` | 实际执行/发送尝试计数；纯查询、幂等重放和投影修复不能凭空增加。 |
| `lease_owner` | `VARCHAR(128)` | 是 | — | 当前领取者身份，非持有期为空。 |
| `lease_until` | `BIGINT` | 是 | — | 领取到期 UTC 毫秒时间；与所有者和代数同时检查。 |
| `receipt_ref` | `TEXT` | 是 | — | 工具/Runner/Outbox 的可信回执 JSON，格式见本表说明及 13.8。 |
| `revision` | `BIGINT` | 否 | `0` | 乐观并发版本，CAS 成功后递增；不等于内容发布序号。 |
| `created_at` | `BIGINT` | 否 | `0` | 创建 UTC 毫秒时间；服务显式填写 StorageClock，不把历史默认 0 当有效时间。 |
| `updated_at` | `BIGINT` | 否 | `0` | 最后持久变更 UTC 毫秒时间；由服务显式更新，无自动更新时间触发器。 |

- PK：`(outbox_id)`。
- UK：`(team_id, outbox_id)`。
- FK：`(team_id) → (teams.team_id)`，删除 RESTRICT；`(team_id, action_id) → (actions.team_id, actions.action_id)`，删除 RESTRICT。
- 显式索引：`ix_outbox_claim(status, available_at)`。

#### 13.2.36 `action_policy_state`：部署动作策略单例

系统级表无 team_id；policy_id 固定 deployment，策略内部逐项限定 team/agent/主体/工具/目标。CLI CAS 同步，Dispatcher 读取数据库权威版本。

| 字段 | 类型 | NULL | 数据库默认 | 含义 |
| --- | --- | --- | --- | --- |
| `policy_id` | `VARCHAR(16)` | 否 | — | 部署单例键，CHECK 固定为 deployment。 |
| `revision` | `BIGINT` | 否 | — | 乐观并发版本，CAS 成功后递增；不等于内容发布序号。 |
| `policy_hash` | `VARCHAR(64)` | 否 | — | 规范化授权策略的 SHA-256，发送/审批时重新核对。 |
| `policy_json` | `TEXT` | 否 | — | 完整部署策略 JSON；条目显式限定租户/Agent，不存凭据值。 |
| `updated_at` | `BIGINT` | 否 | — | 最后持久变更 UTC 毫秒时间；由服务显式更新，无自动更新时间触发器。 |

- PK：`(policy_id)`。
- UK：无额外唯一约束。
- FK：无物理外键。
- 显式索引：无；不含 PK/UK 自动索引。
- CHECK：`policy_id = 'deployment'`。

#### 13.2.37 `incidents`：Duty Event

当前只投影 synthetic-alert-v2，conclusion={rule,status:alert_observed}、severity=unknown；字段存在不表示真实业务根因已验收。

| 字段 | 类型 | NULL | 数据库默认 | 含义 |
| --- | --- | --- | --- | --- |
| `team_id` | `VARCHAR(36)` | 否 | — | 所属团队 UUID；租户隔离边界，不由模型或工具输出改写。 |
| `incident_id` | `VARCHAR(36)` | 否 | — | Duty Event 事件 UUID。 |
| `run_id` | `VARCHAR(36)` | 否 | — | 关联 Run UUID；所有读写同时限定 team_id 并检查业务归属。 |
| `source` | `VARCHAR(128)` | 否 | — | 证据/事件来源键，按来源协议解释。 |
| `service` | `VARCHAR(128)` | 否 | — | 业务服务标识。 |
| `conclusion` | `TEXT` | 否 | — | 事件结构化结论 JSON，不得把未验证模型自由文本直接当结论。 |
| `evidence` | `TEXT` | 否 | — | 事件证据关联 JSON；当前合成投影包含 scope_id/source_hash。 |
| `severity` | `VARCHAR(32)` | 否 | — | 事件严重程度；未确认时保持 unknown。 |
| `created_at` | `BIGINT` | 否 | `0` | 创建 UTC 毫秒时间；服务显式填写 StorageClock，不把历史默认 0 当有效时间。 |
| `updated_at` | `BIGINT` | 否 | `0` | 最后持久变更 UTC 毫秒时间；由服务显式更新，无自动更新时间触发器。 |

- PK：`(incident_id)`。
- UK：`(team_id, incident_id)`。
- FK：`(team_id) → (teams.team_id)`，删除 RESTRICT；`(team_id, run_id) → (runs.team_id, runs.run_id)`，删除 RESTRICT。
- 显式索引：`ix_incidents_service_time(team_id, service, created_at)`。

#### 13.2.38 `incident_links`：事件外部关联

必须有可信外部回执才能登记已创建/已通知。proposed Action 不是外部单据；external_id 不能由模型编造。

| 字段 | 类型 | NULL | 数据库默认 | 含义 |
| --- | --- | --- | --- | --- |
| `team_id` | `VARCHAR(36)` | 否 | — | 所属团队 UUID；租户隔离边界，不由模型或工具输出改写。 |
| `link_id` | `VARCHAR(36)` | 否 | — | 事件外部关联 UUID。 |
| `incident_id` | `VARCHAR(36)` | 否 | — | Duty Event 事件 UUID。 |
| `run_id` | `VARCHAR(36)` | 是 | — | 关联 Run UUID；所有读写同时限定 team_id 并检查业务归属。 |
| `external_system` | `VARCHAR(128)` | 否 | — | 外部系统/连接器标识。 |
| `external_id` | `VARCHAR(128)` | 否 | — | 由下游可信回执确认的工单、跟进或通知标识。 |
| `created_at` | `BIGINT` | 否 | `0` | 创建 UTC 毫秒时间；服务显式填写 StorageClock，不把历史默认 0 当有效时间。 |
| `updated_at` | `BIGINT` | 否 | `0` | 最后持久变更 UTC 毫秒时间；由服务显式更新，无自动更新时间触发器。 |

- PK：`(link_id)`。
- UK：`(incident_id, external_system, external_id)`；`(team_id, link_id)`。
- FK：`(team_id) → (teams.team_id)`，删除 RESTRICT；`(team_id, incident_id) → (incidents.team_id, incidents.incident_id)`，删除 RESTRICT；`(team_id, run_id) → (runs.team_id, runs.run_id)`，删除 RESTRICT。
- 显式索引：无；不含 PK/UK 自动索引。

#### 13.2.39 `incident_action_links`：关联回执来源证明

link_id 是 PK，team_id/action_id 唯一；证明某外部关联由该 Action 产生。Incident/Action/Run 同属关系还需服务校验。

| 字段 | 类型 | NULL | 数据库默认 | 含义 |
| --- | --- | --- | --- | --- |
| `team_id` | `VARCHAR(36)` | 否 | — | 所属团队 UUID；租户隔离边界，不由模型或工具输出改写。 |
| `link_id` | `VARCHAR(36)` | 否 | — | 事件外部关联 UUID。 |
| `action_id` | `VARCHAR(36)` | 否 | — | 逻辑外部动作 UUID；也是提交给下游的稳定幂等身份。 |

- PK：`(link_id)`。
- UK：`(team_id, action_id)`；`(team_id, link_id)`。
- FK：`(team_id, action_id) → (actions.team_id, actions.action_id)`，删除 RESTRICT；`(team_id, link_id) → (incident_links.team_id, incident_links.link_id)`，删除 RESTRICT。
- 显式索引：无；不含 PK/UK 自动索引。

#### 13.2.40 `schedules`：巡检配置

status=active/paused，当前 overlap_policy 仅 forbid；payload 固定 Workflow 版本/hash，但运行仍重验当前授权。next_due_at 和触发写入使用 revision CAS。

| 字段 | 类型 | NULL | 数据库默认 | 含义 |
| --- | --- | --- | --- | --- |
| `team_id` | `VARCHAR(36)` | 否 | — | 所属团队 UUID；租户隔离边界，不由模型或工具输出改写。 |
| `schedule_id` | `VARCHAR(36)` | 否 | — | 所属定时配置 UUID。 |
| `agent_id` | `VARCHAR(36)` | 否 | — | 所属 Agent UUID；关联须同时校验团队和资源作用域。 |
| `cron` | `VARCHAR(128)` | 否 | — | 已校验 cron 表达式。 |
| `timezone` | `VARCHAR(128)` | 否 | — | IANA 时区；所有标量 due_at 仍统一存 UTC 毫秒。 |
| `overlap_policy` | `VARCHAR(32)` | 否 | `'forbid'` | 调度重叠策略，当前仅支持 forbid。 |
| `next_due_at` | `BIGINT` | 是 | — | 下一次业务触发的 UTC 毫秒时间。 |
| `status` | `VARCHAR(32)` | 否 | — | 本表领域状态，具体语义见表说明及第 14 节；数据库不是原生 Enum。 |
| `payload` | `TEXT` | 否 | — | 版本化正文；具体格式和可信边界见本表说明及 13.8。 |
| `revision` | `BIGINT` | 否 | `0` | 乐观并发版本，CAS 成功后递增；不等于内容发布序号。 |
| `created_at` | `BIGINT` | 否 | `0` | 创建 UTC 毫秒时间；服务显式填写 StorageClock，不把历史默认 0 当有效时间。 |
| `updated_at` | `BIGINT` | 否 | `0` | 最后持久变更 UTC 毫秒时间；由服务显式更新，无自动更新时间触发器。 |

- PK：`(schedule_id)`。
- UK：`(team_id, schedule_id)`。
- FK：`(team_id) → (teams.team_id)`，删除 RESTRICT；`(team_id, agent_id) → (agents.team_id, agents.agent_id)`，删除 RESTRICT。
- 显式索引：无；不含 PK/UK 自动索引。

#### 13.2.41 `schedule_fires`：定时触发历史与去重

schedule_id/due_at 唯一，run_id 仅在真实受理后填写；当前状态为 started/missed/overlap_skipped/denied/unavailable，分别记录受理、错过、重叠跳过、拒绝和不可用。

| 字段 | 类型 | NULL | 数据库默认 | 含义 |
| --- | --- | --- | --- | --- |
| `team_id` | `VARCHAR(36)` | 否 | — | 所属团队 UUID；租户隔离边界，不由模型或工具输出改写。 |
| `fire_id` | `VARCHAR(36)` | 否 | — | 一次定时或手动触发记录 UUID。 |
| `schedule_id` | `VARCHAR(36)` | 否 | — | 所属定时配置 UUID。 |
| `due_at` | `BIGINT` | 否 | — | 本次确定的 UTC 毫秒业务触发锚点，不是扫描时间。 |
| `run_id` | `VARCHAR(36)` | 是 | — | 关联 Run UUID；所有读写同时限定 team_id 并检查业务归属。 |
| `status` | `VARCHAR(32)` | 否 | — | 本表领域状态，具体语义见表说明及第 14 节；数据库不是原生 Enum。 |
| `created_at` | `BIGINT` | 否 | `0` | 创建 UTC 毫秒时间；服务显式填写 StorageClock，不把历史默认 0 当有效时间。 |
| `updated_at` | `BIGINT` | 否 | `0` | 最后持久变更 UTC 毫秒时间；由服务显式更新，无自动更新时间触发器。 |

- PK：`(fire_id)`。
- UK：`(schedule_id, due_at)`；`(team_id, fire_id)`。
- FK：`(team_id) → (teams.team_id)`，删除 RESTRICT；`(team_id, run_id) → (runs.team_id, runs.run_id)`，删除 RESTRICT；`(team_id, schedule_id) → (schedules.team_id, schedules.schedule_id)`，删除 RESTRICT。
- 显式索引：无；不含 PK/UK 自动索引。

#### 13.2.42 `schedule_manual_fires`：手动触发历史

独立记录 actor，不伪造定时 due_at；同团队 Run 只能关联一次手动触发。

| 字段 | 类型 | NULL | 数据库默认 | 含义 |
| --- | --- | --- | --- | --- |
| `team_id` | `VARCHAR(36)` | 否 | — | 所属团队 UUID；租户隔离边界，不由模型或工具输出改写。 |
| `fire_id` | `VARCHAR(36)` | 否 | — | 一次定时或手动触发记录 UUID。 |
| `schedule_id` | `VARCHAR(36)` | 否 | — | 所属定时配置 UUID。 |
| `run_id` | `VARCHAR(36)` | 否 | — | 关联 Run UUID；所有读写同时限定 team_id 并检查业务归属。 |
| `actor` | `VARCHAR(128)` | 否 | — | 经过身份验证的操作主体，不存认证 Token。 |
| `created_at` | `BIGINT` | 否 | — | 创建 UTC 毫秒时间；服务显式填写 StorageClock，不把历史默认 0 当有效时间。 |

- PK：`(fire_id)`。
- UK：`(team_id, fire_id)`；`(team_id, run_id)`。
- FK：`(team_id, run_id) → (runs.team_id, runs.run_id)`，删除 RESTRICT；`(team_id, schedule_id) → (schedules.team_id, schedules.schedule_id)`，删除 RESTRICT。
- 显式索引：无；不含 PK/UK 自动索引。

#### 13.2.43 `feedback`：用户反馈

当前 problem_type=user_feedback，payload 含 subject/rating/comment；追加后独立于主回答和观测系统，外部评估失败不丢本地反馈。

| 字段 | 类型 | NULL | 数据库默认 | 含义 |
| --- | --- | --- | --- | --- |
| `team_id` | `VARCHAR(36)` | 否 | — | 所属团队 UUID；租户隔离边界，不由模型或工具输出改写。 |
| `feedback_id` | `VARCHAR(36)` | 否 | — | 用户反馈记录 UUID。 |
| `run_id` | `VARCHAR(36)` | 否 | — | 关联 Run UUID；所有读写同时限定 team_id 并检查业务归属。 |
| `source_trace` | `TEXT` | 否 | — | 来源轨迹 JSON；至少关联 Run，知识问题还需关联真实调用/证据。 |
| `problem_type` | `VARCHAR(32)` | 否 | — | 反馈或知识问题分类；工具不可用不能直接记为知识缺失。 |
| `review_status` | `VARCHAR(32)` | 否 | — | 人工审核状态；pending 不能当作已审核知识或质量真值。 |
| `payload` | `TEXT` | 否 | — | 版本化正文；具体格式和可信边界见本表说明及 13.8。 |
| `created_at` | `BIGINT` | 否 | `0` | 创建 UTC 毫秒时间；服务显式填写 StorageClock，不把历史默认 0 当有效时间。 |
| `updated_at` | `BIGINT` | 否 | `0` | 最后持久变更 UTC 毫秒时间；由服务显式更新，无自动更新时间触发器。 |

- PK：`(feedback_id)`。
- UK：`(team_id, feedback_id)`。
- FK：`(team_id) → (teams.team_id)`，删除 RESTRICT；`(team_id, run_id) → (runs.team_id, runs.run_id)`，删除 RESTRICT。
- 显式索引：无；不含 PK/UK 自动索引。

#### 13.2.44 `knowledge_gaps`：知识缺口候选

表已定义，不能据此宣称检索评估闭环已完成。source_trace 必须对应真实检索，区分未命中/冲突/过期/不可用；发布前审核。

| 字段 | 类型 | NULL | 数据库默认 | 含义 |
| --- | --- | --- | --- | --- |
| `team_id` | `VARCHAR(36)` | 否 | — | 所属团队 UUID；租户隔离边界，不由模型或工具输出改写。 |
| `gap_id` | `VARCHAR(36)` | 否 | — | 待审核知识缺口记录 UUID。 |
| `run_id` | `VARCHAR(36)` | 否 | — | 关联 Run UUID；所有读写同时限定 team_id 并检查业务归属。 |
| `source_trace` | `TEXT` | 否 | — | 来源轨迹 JSON；至少关联 Run，知识问题还需关联真实调用/证据。 |
| `problem_type` | `VARCHAR(32)` | 否 | — | 反馈或知识问题分类；工具不可用不能直接记为知识缺失。 |
| `review_status` | `VARCHAR(32)` | 否 | — | 人工审核状态；pending 不能当作已审核知识或质量真值。 |
| `summary` | `TEXT` | 否 | — | 证据简述/摘要或知识问题描述；不保证是完整可读原文。 |
| `created_at` | `BIGINT` | 否 | `0` | 创建 UTC 毫秒时间；服务显式填写 StorageClock，不把历史默认 0 当有效时间。 |
| `updated_at` | `BIGINT` | 否 | `0` | 最后持久变更 UTC 毫秒时间；由服务显式更新，无自动更新时间触发器。 |

- PK：`(gap_id)`。
- UK：`(team_id, gap_id)`。
- FK：`(team_id) → (teams.team_id)`，删除 RESTRICT；`(team_id, run_id) → (runs.team_id, runs.run_id)`，删除 RESTRICT。
- 显式索引：无；不含 PK/UK 自动索引。

#### 13.2.45 `idempotency_records`：Agent 幂等回执

作用域为 team/subject/agent/route/key；同键不同 request_hash 冲突。业务记录、HTTP 响应体及状态码同事务提交。

| 字段 | 类型 | NULL | 数据库默认 | 含义 |
| --- | --- | --- | --- | --- |
| `team_id` | `VARCHAR(36)` | 否 | — | 所属团队 UUID；租户隔离边界，不由模型或工具输出改写。 |
| `record_id` | `VARCHAR(36)` | 否 | — | 幂等响应或用量结算记录 UUID。 |
| `agent_id` | `VARCHAR(36)` | 否 | — | 所属 Agent UUID；关联须同时校验团队和资源作用域。 |
| `subject` | `VARCHAR(128)` | 否 | — | 身份系统主体字符串，不要求为 UUID，不存认证 Token。 |
| `route` | `VARCHAR(128)` | 否 | — | 规范化幂等操作键，可为领域操作名，不必等于请求 URL。 |
| `key` | `VARCHAR(128)` | 否 | — | 调用方幂等键，不是数据库主键。 |
| `request_hash` | `VARCHAR(64)` | 否 | — | 规范化请求 SHA-256；同一幂等身份出现不同摘要必须冲突。 |
| `response_ref` | `TEXT` | 否 | — | 原提交响应或回复映射的规范化 JSON，不是任意缓存地址。 |
| `status_code` | `BIGINT` | 否 | — | 原幂等响应的 HTTP 状态码。 |
| `expires_at` | `BIGINT` | 否 | — | 到期 UTC 毫秒时间；到期不代表已经物理删除。 |
| `created_at` | `BIGINT` | 否 | `0` | 创建 UTC 毫秒时间；服务显式填写 StorageClock，不把历史默认 0 当有效时间。 |
| `updated_at` | `BIGINT` | 否 | `0` | 最后持久变更 UTC 毫秒时间；由服务显式更新，无自动更新时间触发器。 |

- PK：`(record_id)`。
- UK：`(team_id, record_id)`；`(team_id, subject, agent_id, route, key)`。
- FK：`(team_id) → (teams.team_id)`，删除 RESTRICT；`(team_id, agent_id) → (agents.team_id, agents.agent_id)`，删除 RESTRICT。
- 显式索引：`ix_idempotency_expiry(expires_at)`。

#### 13.2.46 `team_idempotency_records`：团队级幂等回执

作用域不含 Agent，用于团队/成员管理及创建 Agent；不能借用已有默认 Agent 的幂等范围，也不能与 Agent 幂等回执表混用。Agent 创建与团队级原始响应回执在同事务保存；重放仍检查当前成员、角色及 agent_scope，不能借旧回执绕过当前范围限制。

| 字段 | 类型 | NULL | 数据库默认 | 含义 |
| --- | --- | --- | --- | --- |
| `team_id` | `VARCHAR(36)` | 否 | — | 所属团队 UUID；租户隔离边界，不由模型或工具输出改写。 |
| `record_id` | `VARCHAR(36)` | 否 | — | 幂等响应或用量结算记录 UUID。 |
| `subject` | `VARCHAR(128)` | 否 | — | 身份系统主体字符串，不要求为 UUID，不存认证 Token。 |
| `route` | `VARCHAR(128)` | 否 | — | 规范化幂等操作键，可为领域操作名，不必等于请求 URL。 |
| `key` | `VARCHAR(128)` | 否 | — | 调用方幂等键，不是数据库主键。 |
| `request_hash` | `VARCHAR(64)` | 否 | — | 规范化请求 SHA-256；同一幂等身份出现不同摘要必须冲突。 |
| `response_ref` | `TEXT` | 否 | — | 原提交响应或回复映射的规范化 JSON，不是任意缓存地址。 |
| `status_code` | `BIGINT` | 否 | — | 原幂等响应的 HTTP 状态码。 |
| `expires_at` | `BIGINT` | 否 | — | 到期 UTC 毫秒时间；到期不代表已经物理删除。 |
| `created_at` | `BIGINT` | 否 | `0` | 创建 UTC 毫秒时间；服务显式填写 StorageClock，不把历史默认 0 当有效时间。 |
| `updated_at` | `BIGINT` | 否 | `0` | 最后持久变更 UTC 毫秒时间；由服务显式更新，无自动更新时间触发器。 |

- PK：`(record_id)`。
- UK：`(team_id, record_id)`；`(team_id, subject, route, key)`。
- FK：`(team_id) → (teams.team_id)`，删除 RESTRICT。
- 显式索引：`ix_team_idempotency_expiry(expires_at)`。

#### 13.2.47 `source_events`：入口事件去重和回复关联

同 team/connector/external_event 唯一，response_ref 可存 Run/Thread 受理或回复映射。当前没有独立 run_id/thread_id/engine 列，不能独自完成跨引擎切流。

| 字段 | 类型 | NULL | 数据库默认 | 含义 |
| --- | --- | --- | --- | --- |
| `team_id` | `VARCHAR(36)` | 否 | — | 所属团队 UUID；租户隔离边界，不由模型或工具输出改写。 |
| `source_event_id` | `VARCHAR(36)` | 否 | — | 本地入口事件记录 UUID，不是外部事件 ID。 |
| `connector_id` | `VARCHAR(128)` | 否 | — | 部署注册的来源连接器键。 |
| `external_event_id` | `VARCHAR(128)` | 否 | — | 外部事件标识，须包含来源协议规定的去重作用域。 |
| `request_hash` | `VARCHAR(64)` | 否 | — | 规范化请求 SHA-256；同一幂等身份出现不同摘要必须冲突。 |
| `response_ref` | `TEXT` | 是 | — | 原提交响应或回复映射的规范化 JSON，不是任意缓存地址。 |
| `expires_at` | `BIGINT` | 否 | — | 到期 UTC 毫秒时间；到期不代表已经物理删除。 |
| `created_at` | `BIGINT` | 否 | `0` | 创建 UTC 毫秒时间；服务显式填写 StorageClock，不把历史默认 0 当有效时间。 |
| `updated_at` | `BIGINT` | 否 | `0` | 最后持久变更 UTC 毫秒时间；由服务显式更新，无自动更新时间触发器。 |

- PK：`(source_event_id)`。
- UK：`(team_id, connector_id, external_event_id)`；`(team_id, source_event_id)`。
- FK：`(team_id) → (teams.team_id)`，删除 RESTRICT。
- 显式索引：无；不含 PK/UK 自动索引。

#### 13.2.48 `audit_logs`：追加审计

业务 Store 不提供修改已发布审计行；不是数据库触发器级不可篡改保证。decision 保存 allowed/denied 或固定执行结果，trace 仅脱敏关联。

| 字段 | 类型 | NULL | 数据库默认 | 含义 |
| --- | --- | --- | --- | --- |
| `team_id` | `VARCHAR(36)` | 否 | — | 所属团队 UUID；租户隔离边界，不由模型或工具输出改写。 |
| `audit_id` | `VARCHAR(36)` | 否 | — | 审计记录 UUID。 |
| `actor` | `VARCHAR(128)` | 否 | — | 经过身份验证的操作主体，不存认证 Token。 |
| `object` | `TEXT` | 否 | — | 审计目标资源 ID 或规范化对象描述。 |
| `operation` | `VARCHAR(128)` | 否 | — | 稳定审计操作名，如 action.propose。 |
| `decision` | `VARCHAR(128)` | 否 | — | 审批/审计决定；准确取值和含义见本表说明。 |
| `trace` | `TEXT` | 否 | — | 脱敏审计关联 JSON，禁止凭据和模型内部思考。 |
| `created_at` | `BIGINT` | 否 | `0` | 创建 UTC 毫秒时间；服务显式填写 StorageClock，不把历史默认 0 当有效时间。 |
| `updated_at` | `BIGINT` | 否 | `0` | 最后持久变更 UTC 毫秒时间；由服务显式更新，无自动更新时间触发器。 |

- PK：`(audit_id)`。
- UK：`(team_id, audit_id)`。
- FK：`(team_id) → (teams.team_id)`，删除 RESTRICT。
- 显式索引：`ix_audit_time(team_id, created_at)`。

#### 13.2.49 `admin_jobs`：持久管理任务

kind 为 model_probe/tool_probe/repo_sync/skill_import/artifact_upload 等。payload 按类型验证，result_ref 保存结果；与 Run Job 分开领取和授权，guard_revision 独立 fencing。

| 字段 | 类型 | NULL | 数据库默认 | 含义 |
| --- | --- | --- | --- | --- |
| `team_id` | `VARCHAR(36)` | 否 | — | 所属团队 UUID；租户隔离边界，不由模型或工具输出改写。 |
| `job_id` | `VARCHAR(36)` | 否 | — | 本表所属或关联的 Job UUID；管理任务与 Run Job 使用不同表。 |
| `agent_id` | `VARCHAR(36)` | 否 | — | 所属 Agent UUID；关联须同时校验团队和资源作用域。 |
| `subject` | `VARCHAR(128)` | 否 | — | 身份系统主体字符串，不要求为 UUID，不存认证 Token。 |
| `kind` | `VARCHAR(32)` | 否 | — | 类型分辨字段，按本表对应领域 DTO 校验。 |
| `status` | `VARCHAR(32)` | 否 | — | 本表领域状态，具体语义见表说明及第 14 节；数据库不是原生 Enum。 |
| `payload` | `TEXT` | 否 | — | 版本化正文；具体格式和可信边界见本表说明及 13.8。 |
| `request_hash` | `VARCHAR(64)` | 否 | — | 规范化请求 SHA-256；同一幂等身份出现不同摘要必须冲突。 |
| `idempotency_key` | `VARCHAR(128)` | 否 | — | 调用方幂等键，仅在本表规定作用域内解释。 |
| `idempotency_expires_at` | `BIGINT` | 否 | — | 管理命令幂等有效期的 UTC 毫秒时间。 |
| `lease_owner` | `VARCHAR(128)` | 是 | — | 当前领取者身份，非持有期为空。 |
| `lease_until` | `BIGINT` | 是 | — | 领取到期 UTC 毫秒时间；与所有者和代数同时检查。 |
| `attempt` | `BIGINT` | 否 | `0` | 实际执行/发送尝试计数；纯查询、幂等重放和投影修复不能凭空增加。 |
| `available_at` | `BIGINT` | 否 | — | 允许领取或重试的最早 UTC 毫秒时间。 |
| `result_ref` | `TEXT` | 是 | — | 已校验管理任务结果/错误 JSON。 |
| `revision` | `BIGINT` | 否 | `0` | 乐观并发版本，CAS 成功后递增；不等于内容发布序号。 |
| `guard_revision` | `BIGINT` | 否 | `0` | 租约或管理任务领取的内部 CAS 版本，不是公开 Run revision。 |
| `last_event_seq` | `BIGINT` | 否 | `0` | 该 Run/管理任务已分配的最高持久事件序号。 |
| `created_at` | `BIGINT` | 否 | `0` | 创建 UTC 毫秒时间；服务显式填写 StorageClock，不把历史默认 0 当有效时间。 |
| `updated_at` | `BIGINT` | 否 | `0` | 最后持久变更 UTC 毫秒时间；由服务显式更新，无自动更新时间触发器。 |

- PK：`(job_id)`。
- UK：`(team_id, job_id)`；`(team_id, agent_id, subject, kind, idempotency_key)`。
- FK：`(team_id) → (teams.team_id)`，删除 RESTRICT；`(team_id, agent_id) → (agents.team_id, agents.agent_id)`，删除 RESTRICT。
- 显式索引：`ix_admin_jobs_claim(status, available_at)`。

#### 13.2.50 `admin_job_events`：管理任务事件

与 admin_jobs.last_event_seq 同事务分配；不能与 Run SSE 共用序号空间。payload 仅脱敏管理事件 JSON。

| 字段 | 类型 | NULL | 数据库默认 | 含义 |
| --- | --- | --- | --- | --- |
| `team_id` | `VARCHAR(36)` | 否 | — | 所属团队 UUID；租户隔离边界，不由模型或工具输出改写。 |
| `job_id` | `VARCHAR(36)` | 否 | — | 本表所属或关联的 Job UUID；管理任务与 Run Job 使用不同表。 |
| `seq` | `BIGINT` | 否 | — | 当前 Run 或管理任务内部从 1 开始的单调事件序号。 |
| `type` | `VARCHAR(128)` | 否 | — | 公开版本化事件类型，Run 事件见第 12.2 节。 |
| `payload` | `TEXT` | 否 | — | 版本化正文；具体格式和可信边界见本表说明及 13.8。 |
| `occurred_at` | `BIGINT` | 否 | — | 持久事件所用 UTC 毫秒时间。 |

- PK：`(job_id, seq)`。
- UK：无额外唯一约束。
- FK：`(team_id, job_id) → (admin_jobs.team_id, admin_jobs.job_id)`，删除 RESTRICT。
- 显式索引：`ix_admin_events_time(occurred_at)`。

#### 13.2.51 `synthetic_alert_sources`：合成告警来源快照

仅 development/test 使用；payload 为已签名 synthetic-alert-v2，payload_hash 是规范化来源摘要。identity_id 为空的历史来源拒绝使用，不补猜身份。

| 字段 | 类型 | NULL | 数据库默认 | 含义 |
| --- | --- | --- | --- | --- |
| `team_id` | `VARCHAR(36)` | 否 | — | 所属团队 UUID；租户隔离边界，不由模型或工具输出改写。 |
| `run_id` | `VARCHAR(36)` | 否 | — | 关联 Run UUID；所有读写同时限定 team_id 并检查业务归属。 |
| `connector_id` | `VARCHAR(128)` | 否 | — | 部署注册的来源连接器键。 |
| `payload` | `TEXT` | 否 | — | 版本化正文；具体格式和可信边界见本表说明及 13.8。 |
| `payload_hash` | `VARCHAR(64)` | 否 | — | 规范化正文或动作参数的 SHA-256；动作算法见 13.8。 |
| `created_at` | `BIGINT` | 否 | — | 创建 UTC 毫秒时间；服务显式填写 StorageClock，不把历史默认 0 当有效时间。 |
| `identity_id` | `VARCHAR(128)` | 是 | — | 精确部署服务身份键；历史合成来源可空，空时不得补猜身份。 |

- PK：`(run_id)`。
- UK：`(team_id, run_id)`。
- FK：`(team_id, run_id) → (runs.team_id, runs.run_id)`，删除 RESTRICT。
- 显式索引：无；不含 PK/UK 自动索引。

#### 13.2.52 `synthetic_result_bindings`：合成结果绑定

固定恢复候选摘要与最终校正/脱敏投影摘要，防止重放接受另一个候选。

| 字段 | 类型 | NULL | 数据库默认 | 含义 |
| --- | --- | --- | --- | --- |
| `team_id` | `VARCHAR(36)` | 否 | — | 所属团队 UUID；租户隔离边界，不由模型或工具输出改写。 |
| `run_id` | `VARCHAR(36)` | 否 | — | 关联 Run UUID；所有读写同时限定 team_id 并检查业务归属。 |
| `candidate_digest` | `VARCHAR(64)` | 否 | — | 图产生的候选 Outcome 的 SHA-256。 |
| `projection_digest` | `VARCHAR(64)` | 否 | — | 最终范围校正/脱敏后持久 Outcome 的 SHA-256。 |

- PK：`(run_id)`。
- UK：`(team_id, run_id)`。
- FK：`(team_id, run_id) → (runs.team_id, runs.run_id)`，删除 RESTRICT。
- 显式索引：无；不含 PK/UK 自动索引。

#### 13.2.53 `synthetic_source_policy`：合成来源授权单例

policy_id=deployment；仅为开发测试来源 grant 的数据库权威快照，不替代生产真实协议授权。

| 字段 | 类型 | NULL | 数据库默认 | 含义 |
| --- | --- | --- | --- | --- |
| `policy_id` | `VARCHAR(16)` | 否 | — | 部署单例键，CHECK 固定为 deployment。 |
| `revision` | `BIGINT` | 否 | — | 乐观并发版本，CAS 成功后递增；不等于内容发布序号。 |
| `policy_hash` | `VARCHAR(64)` | 否 | — | 规范化授权策略的 SHA-256，发送/审批时重新核对。 |
| `policy_json` | `TEXT` | 否 | — | 完整部署策略 JSON；条目显式限定租户/Agent，不存凭据值。 |
| `updated_at` | `BIGINT` | 否 | — | 最后持久变更 UTC 毫秒时间；由服务显式更新，无自动更新时间触发器。 |

- PK：`(policy_id)`。
- UK：无额外唯一约束。
- FK：无物理外键。
- 显式索引：无；不含 PK/UK 自动索引。
- CHECK：`policy_id = 'deployment'`。

#### 13.2.54 `synthetic_proposal_policy`：合成单动作规则单例

dod.v14 可信提议规则；模型正文不能注册任意动作、目标或审批人。

| 字段 | 类型 | NULL | 数据库默认 | 含义 |
| --- | --- | --- | --- | --- |
| `policy_id` | `VARCHAR(16)` | 否 | — | 部署单例键，CHECK 固定为 deployment。 |
| `revision` | `BIGINT` | 否 | — | 乐观并发版本，CAS 成功后递增；不等于内容发布序号。 |
| `policy_hash` | `VARCHAR(64)` | 否 | — | 规范化授权策略的 SHA-256，发送/审批时重新核对。 |
| `policy_json` | `TEXT` | 否 | — | 完整部署策略 JSON；条目显式限定租户/Agent，不存凭据值。 |
| `updated_at` | `BIGINT` | 否 | — | 最后持久变更 UTC 毫秒时间；由服务显式更新，无自动更新时间触发器。 |

- PK：`(policy_id)`。
- UK：无额外唯一约束。
- FK：无物理外键。
- 显式索引：无；不含 PK/UK 自动索引。
- CHECK：`policy_id = 'deployment'`。

#### 13.2.55 `synthetic_batch_policy`：合成多动作规则单例

dod.v15 有界多动作规则，与单动作策略分别维护 revision/hash。

| 字段 | 类型 | NULL | 数据库默认 | 含义 |
| --- | --- | --- | --- | --- |
| `policy_id` | `VARCHAR(16)` | 否 | — | 部署单例键，CHECK 固定为 deployment。 |
| `revision` | `BIGINT` | 否 | — | 乐观并发版本，CAS 成功后递增；不等于内容发布序号。 |
| `policy_hash` | `VARCHAR(64)` | 否 | — | 规范化授权策略的 SHA-256，发送/审批时重新核对。 |
| `policy_json` | `TEXT` | 否 | — | 完整部署策略 JSON；条目显式限定租户/Agent，不存凭据值。 |
| `updated_at` | `BIGINT` | 否 | — | 最后持久变更 UTC 毫秒时间；由服务显式更新，无自动更新时间触发器。 |

- PK：`(policy_id)`。
- UK：无额外唯一约束。
- FK：无物理外键。
- 显式索引：无；不含 PK/UK 自动索引。
- CHECK：`policy_id = 'deployment'`。

#### 13.2.56 `synthetic_action_policy_bindings`：合成动作策略绑定

每 Action 固定来源与提议策略 revision/hash；发送前当前策略不匹配则拒绝，不能审批后更换规则。

| 字段 | 类型 | NULL | 数据库默认 | 含义 |
| --- | --- | --- | --- | --- |
| `team_id` | `VARCHAR(36)` | 否 | — | 所属团队 UUID；租户隔离边界，不由模型或工具输出改写。 |
| `action_id` | `VARCHAR(36)` | 否 | — | 逻辑外部动作 UUID；也是提交给下游的稳定幂等身份。 |
| `source_revision` | `BIGINT` | 否 | — | 创建动作时来源策略修订号。 |
| `source_hash` | `VARCHAR(64)` | 否 | — | 创建动作时来源策略 SHA-256。 |
| `proposal_revision` | `BIGINT` | 否 | — | 创建动作时单动作/多动作规则修订号。 |
| `proposal_hash` | `VARCHAR(64)` | 否 | — | 创建动作时提议规则的 SHA-256。 |

- PK：`(action_id)`。
- UK：`(team_id, action_id)`。
- FK：`(team_id, action_id) → (actions.team_id, actions.action_id)`，删除 RESTRICT。
- 显式索引：无；不含 PK/UK 自动索引。

#### 13.2.57 `dod_storage_metadata`：协议元数据单例

协议元数据的单例约束沿用本节物理声明；ready 要求恰好一行，并匹配新源码声明的存储协议和 Codec。旧基线为 dod-storage-v1/dod-checkpoint-v1，不能据此允许 Workspace 旧库进入 Agent 运行时；这些标记均不等于 Alembic revision。

| 字段 | 类型 | NULL | 数据库默认 | 含义 |
| --- | --- | --- | --- | --- |
| `schema_version` | `TEXT` | 否 | — | 版本标记；事件信封为字符串 1，与存储协议 dod-storage-agent-v1 分开。 |
| `checkpoint_codec_version` | `TEXT` | 否 | — | 检查点编码协议标记，当前 dod-checkpoint-v1。 |

- PK：无；单例校验见表说明。
- UK：无额外唯一约束。
- FK：无物理外键。
- 显式索引：无；不含 PK/UK 自动索引。

#### 13.2.58 `graph_checkpoints`：图检查点快照

namespace 原文与摘要同时保存；parent 必须同 Run/namespace。payload 保存完整 typed checkpoint bytes，含 channel_versions/versions_seen；metadata 也保留 Codec 字节。

| 字段 | 类型 | NULL | 数据库默认 | 含义 |
| --- | --- | --- | --- | --- |
| `team_id` | `VARCHAR(36)` | 否 | — | 所属团队 UUID；租户隔离边界，不由模型或工具输出改写。 |
| `run_id` | `VARCHAR(36)` | 否 | — | 关联 Run UUID；所有读写同时限定 team_id 并检查业务归属。 |
| `namespace_hash` | `VARCHAR(64)` | 否 | — | namespace UTF-8 的 SHA-256；仍须比较原文并拒绝哈希碰撞。 |
| `namespace` | `TEXT` | 否 | — | 完整检查点命名空间；主图可为空字符串，专家使用稳定子命名空间。 |
| `checkpoint_id` | `VARCHAR(128)` | 否 | — | 框架生成的可排序检查点标识，不使用数据库自增键。 |
| `parent_id` | `VARCHAR(128)` | 是 | — | 同 Run/namespace 的父 checkpoint_id；链首为空。 |
| `payload_type` | `VARCHAR(128)` | 否 | — | 检查点 Codec 类型标签，与 payload 原始字节配对。 |
| `payload` | `BLOB` | 否 | — | 版本化正文；具体格式和可信边界见本表说明及 13.8。 |
| `metadata_type` | `VARCHAR(128)` | 否 | — | 框架 metadata 的 Codec 类型标签。 |
| `metadata` | `BLOB` | 否 | — | 框架 metadata 的原始 Codec 字节，不按普通 JSON 任意重编码。 |
| `codec_version` | `VARCHAR(128)` | 否 | — | 此检查点字节采用的 Codec 版本。 |
| `run_epoch` | `BIGINT` | 否 | — | Run 执行所有权代数，接管后单调递增以拒绝旧执行写入。 |
| `created_at` | `BIGINT` | 否 | — | 创建 UTC 毫秒时间；服务显式填写 StorageClock，不把历史默认 0 当有效时间。 |

- PK：`(run_id, namespace_hash, checkpoint_id)`。
- UK：`(team_id, run_id, namespace_hash, checkpoint_id)`。
- FK：`(team_id, run_id) → (runs.team_id, runs.run_id)`，删除 RESTRICT；`(team_id, run_id, namespace_hash, parent_id) → (graph_checkpoints.team_id, graph_checkpoints.run_id, graph_checkpoints.namespace_hash, graph_checkpoints.checkpoint_id)`，删除 RESTRICT。
- 显式索引：无；不含 PK/UK 自动索引。

#### 13.2.59 `graph_pending_writes`：节点部分完成写入

父检查点 FK 同时限定团队；普通非负索引重复写保留已有值，特殊负索引按锁定框架映射更新。pending writes 不能简化成最终消息列表。

| 字段 | 类型 | NULL | 数据库默认 | 含义 |
| --- | --- | --- | --- | --- |
| `team_id` | `VARCHAR(36)` | 否 | — | 所属团队 UUID；租户隔离边界，不由模型或工具输出改写。 |
| `run_id` | `VARCHAR(36)` | 否 | — | 关联 Run UUID；所有读写同时限定 team_id 并检查业务归属。 |
| `namespace_hash` | `VARCHAR(64)` | 否 | — | namespace UTF-8 的 SHA-256；仍须比较原文并拒绝哈希碰撞。 |
| `checkpoint_id` | `VARCHAR(128)` | 否 | — | 框架生成的可排序检查点标识，不使用数据库自增键。 |
| `task_id` | `VARCHAR(128)` | 否 | — | LangGraph 任务标识，不是平台 run_jobs.job_id。 |
| `write_index` | `BIGINT` | 否 | — | 同 task 的写入索引，特殊负值保持锁定 LangGraph WRITES_IDX_MAP 语义。 |
| `channel` | `VARCHAR(128)` | 否 | — | LangGraph 写入通道名，包含框架特殊通道。 |
| `payload_type` | `VARCHAR(128)` | 否 | — | 检查点 Codec 类型标签，与 payload 原始字节配对。 |
| `payload` | `BLOB` | 否 | — | 版本化正文；具体格式和可信边界见本表说明及 13.8。 |
| `task_path` | `TEXT` | 否 | — | 框架任务路径，是 pending writes 恢复身份与排序的一部分。 |
| `run_epoch` | `BIGINT` | 否 | — | Run 执行所有权代数，接管后单调递增以拒绝旧执行写入。 |

- PK：`(run_id, namespace_hash, checkpoint_id, task_id, write_index)`。
- UK：无额外唯一约束。
- FK：`(team_id, run_id, namespace_hash, checkpoint_id) → (graph_checkpoints.team_id, graph_checkpoints.run_id, graph_checkpoints.namespace_hash, graph_checkpoints.checkpoint_id)`，删除 RESTRICT。
- 显式索引：无；不含 PK/UK 自动索引。

#### 13.2.60 `skill_drafts`：团队共享技能草稿

由 `0002_shared_skills` 增量迁移新增，不改写既有私有 Skill。草稿与发布版本分离，CAS 复用所属 skills 行的 revision，不另设独立草稿 revision。

| 字段 | 类型 | NULL | 数据库默认 | 含义 |
| --- | --- | --- | --- | --- |
| `skill_id` | `VARCHAR(36)` | 否 | — | 所属 Skill UUID，也是草稿主键。 |
| `team_id` | `VARCHAR(36)` | 否 | — | 所属团队 UUID。 |
| `name` | `VARCHAR(128)` | 否 | — | 团队内唯一、创建后稳定的技能名称。 |
| `description` | `TEXT` | 否 | — | 可编辑描述。 |
| `content` | `TEXT` | 否 | — | 可编辑 Markdown 指令正文。 |
| `created_at` | `BIGINT` | 否 | — | 创建 UTC 毫秒时间。 |
| `updated_at` | `BIGINT` | 否 | — | 最近编辑 UTC 毫秒时间。 |

- PK：`skill_id`。
- UK：`(team_id, name)`；此唯一性属于共享草稿，不声称 skills 表已有同名唯一索引。
- FK：`(team_id, skill_id) → (skills.team_id, skills.skill_id)`，删除 RESTRICT。
- 显式索引：无；不含 PK/UK 自动索引。

#### 13.2.61 `run_trace_contexts`：异步 Trace 上下文

由 `0003_run_trace_contexts` 新增，只保存内部签发的 W3C traceparent，不保存正文或凭据。

| 字段 | 类型 | NULL | 含义 |
| --- | --- | --- | --- |
| `run_id` | `VARCHAR(36)` | 否 | Run 主键。 |
| `team_id` | `VARCHAR(36)` | 否 | 所属团队。 |
| `traceparent` | `VARCHAR(55)` | 否 | Run 受理时固定的追踪上下文。 |
| `created_at` | `BIGINT` | 否 | 创建 UTC 毫秒时间。 |

PK 为 `run_id`；复合外键 `(team_id, run_id)` 指向 runs，删除 RESTRICT。

### 13.3 可移植约束示例

以下 SQL 展示当前已声明的关键索引和恢复 CAS 契约，不是本次要执行的 DDL。完整物理字段见第 13.2 节，待新增约束见第 13.10 节。公共查询不依赖原生 UUID、JSONB、RETURNING 或条件索引，参数占位由 SQLAlchemy 适配。

~~~sql
CREATE UNIQUE INDEX uq_run_job_generation
ON run_jobs(run_id, generation, kind);

CREATE UNIQUE INDEX uq_action_key
ON actions(team_id, action_key);

CREATE UNIQUE INDEX uq_schedule_fire
ON schedule_fires(schedule_id, due_at);

CREATE INDEX ix_claimable_jobs
ON run_jobs(status, available_at, priority, created_at);

CREATE INDEX ix_expired_leases
ON execution_leases(expires_at, owner_run_id);

CREATE UNIQUE INDEX uq_source_event
ON source_events(team_id, connector_id, external_event_id);

UPDATE interrupts
SET accepted_resume_request_id = :request_id,
    status = 'response_submitted'
WHERE interrupt_id = :interrupt_id
  AND status = 'open'
  AND accepted_resume_request_id IS NULL;
~~~

恢复事务先插入 ResumeRequest，再执行条件更新并检查影响行数恰为 1；否则整个事务回滚。accepted_resume_request_id 是有效恢复请求的权威指针，历史请求仍可保留，三种数据库不依赖不同的部分唯一索引语义。

同一 Thread 的活动 Run、执行租约、Run epoch 和 Job 在一个 UnitOfWork 中通过 CAS 更新。SQLite 的 BEGIN IMMEDIATE 串行化短写事务；MySQL/PostgreSQL 在适配器内按固定顺序锁行或条件更新。单靠 API 预检查或 Redis 锁不满足互斥契约。

### 13.4 数据保留

| 数据 | 默认保留 | 处理 |
| --- | --- | --- |
| RunEvent 与增量文本 | 7 天 | 按主键批量清理；超期返回游标过期 |
| Run、消息摘要、工具元数据、费用 | 90 天 | 按团队配置归档或删除 |
| Duty Event 与最终报告 | 180 天 | 依团队政策归档 |
| 原始日志证据/临时产物 | 7 天 | 与 Run 元数据分离，独立授权 |
| 普通报告与最终产物 | 90 天 | 可按事件需要延长 |
| 幂等请求 | 72 小时 | 业务动作唯一键继续存在于动作生命周期内 |
| 来源事件去重 | 90 天 | 7 天以前的接入事件需显式补录通道 |
| 审计记录 | 180 天 | 受限查询，包含删除审计 |
| 检查点 | 活动 Run 全量；终态后 30 天 | 不删除可恢复 Run 的检查点 |
| 已发布 Skill/Profile | 有 Run 引用期间 | 采用引用检查和归档，不能直接清理 |

过期数据可能导致历史 Evidence 仅保留摘要和来源说明；界面必须显示原始材料是否已过期。附件下载使用短时签名 URL，且签发前重新授权。


**清理顺序与引用保护**

上述天数是目标保留策略，不表示当前 Scheduler 已实现全表周期清理。源码已有 RunEvent prune、终态检查点删除、上传暂存和 Runner 资源清理能力；全局保留期、删除审计和对象垃圾回收仍需单独交付。

1. 仅选择终态且无有效执行租约的 Run，小批量提交。活动、等待及可恢复任务的必要版本、检查点、pending writes 不得按普通到期规则删除。
2. 当前没有独立 finished_at 列；终态保留扫描暂以 runs.updated_at 为保守锚点，后续变更只延长保留。不能依赖 7 天后已过期的 SSE 事件推算终态时间；新增 finished_at 需要迁移及历史处理规则。
3. 删除 RunEvent 与推进 runs.event_floor 同事务，不能重置 last_event_seq；慢 SSE 客户端不能持有清理事务。evidence_payloads/原始对象到期后可删除正文，但保留证据摘要、来源和过期说明。
4. 删除终态检查点时，先删 graph_pending_writes，再在同事务清除待删快照 parent_id、删除快照；不能切断仍需保留的父链。执行前再次检查终态和无租约。
5. RESTRICT 引用优先于表面天数：Duty Event 保留 180 天而 Run 默认 90 天时，延长被引用 Run、必要消息/结果和关联元数据的保留，不能第 90 天删掉事件依据。unknown/sending Action、待对账费用、未完成 Runner 清理同样延长必要来源记录。
6. 对象删除按“标记到期并拒绝新下载 → 复核全部有效引用 → 删除对象 → 追加审计”执行。数据库与 S3 没有原子事务，失败保留可重试状态；内容寻址对象仍被活动 Run 或发布版本引用时不删。
7. 真正删除 Run 前按 FK 依赖解除 accepted_resume_request_id、Thread.active_run_id 等循环指针，清理引用该 Run 的辅助记录和消息/任务后再删主行。不能关闭全库 foreign_keys 或依赖未声明的级联删除。


### 13.5 数据类型和方言边界

| 逻辑类型/能力 | SQLite 首版 | MySQL 后续 | PostgreSQL 后续 |
| --- | --- | --- | --- |
| UUID / 状态 / alias | TEXT / VARCHAR，应用校验长度 | CHAR/VARCHAR，机器键使用二进制比较 | CHAR/VARCHAR，保持同一规范 |
| 时间/计数/epoch | 64 位整数，UTC 毫秒 | BIGINT | BIGINT |
| JSON | 规范化 TEXT，经 Pydantic 验证 | LONGTEXT，同一 Codec | TEXT，同一 Codec |
| 检查点字节载荷 | BLOB | LONGBLOB | BYTEA |
| 金额 | 整数微单位 + currency | 同左 | 同左 |
| 事务 | 写操作显式 BEGIN IMMEDIATE | InnoDB，READ COMMITTED | READ COMMITTED |
| 领取 Job | 短事务 CAS，单写者 | CAS 基线，行锁/skip-locked 可优化 | CAS 基线，行锁/skip-locked 可优化 |
| Schema 迁移 | Alembic batch/rebuild，维护窗口 | 方言迁移，考虑 DDL 隐式提交 | 方言迁移 |
| 清理过期记录 | 小批量删除与索引 | 同一逻辑，可使用额外索引优化 | 同一逻辑，可使用分区优化 |

公共查询不使用 JSON 路径、原生 Enum、数据库序列、条件唯一索引或特定 upsert 语法；筛选与索引字段使用显式标量列。唯一性/冲突由约束和受控重试实现，方言 upsert 只允许位于后端内部。

机器标识在入库前规范化，文本载荷保留原文。长复合索引使用固定长度摘要并核对原文，避免 MySQL 索引字节限制和默认大小写比较差异。checkpoint namespace 保存完整原文，同时以 SHA-256 namespace_hash 参与复合主键；哈希冲突必须报错，不能合并不同命名空间。

lease 与 deadline 的权威时间来自 StorageClock，由后端在事务中返回数据库 UTC 毫秒。SQLite 与应用位于同一主机，后续服务型数据库使用服务端时钟，避免不同 Worker 时钟影响接管判断。活跃片段的本地超时使用单调时钟。

### 13.6 统一检查点存储

PortableCheckpointSaver 实现 LangGraph BaseCheckpointSaver 的异步方法：aget_tuple、alist、aput、aput_writes、adelete_thread。它通过 CheckpointStore 和 UnitOfWork 读写 graph_checkpoints / graph_pending_writes，不直接使用只能在某一数据库运行的 Saver。[F10]

- aget_tuple 恢复指定或最新检查点、parent_config 和 pending writes。
- alist 保留 namespace、before、limit 和过滤语义，排序使用 checkpoint_id/已定义顺序，不依赖数据库自增键。
- aput 在执行 Guard 校验通过的同一事务中保存快照和父链；返回框架期望的 checkpoint config。
- aput_writes 在同一 Guard 事务中保存 task/channel/write_index，重复写入语义遵循锁定框架版本的特殊写入索引规则，不能简单覆盖所有 pending writes。
- adelete_thread 只能由经过授权的保留期清理执行，不能删除活动 Run；删除写入、快照和索引的顺序受同一事务保护。

typed payload 使用锁定版本的 SerializerProtocol，分别存 type tag 与 bytes；显式保存 codec_version。迁移复制字节与元数据，不将 BaseMessage、channel_versions、versions_seen、父链或 pending writes 简化成一份 JSON 消息列表。仅允许发布版本支持的类型注册，不从任意用户内容加载可执行序列化对象。

Checkpoint 写入与 Guard 必须共用同一个 UnitOfWork/数据库连接和事务。先查询租约、再用另一个库连接调用原生 Saver，不满足 fencing 条件。原生 SQLite/Postgres Saver 可用于框架行为对照测试，但没有通过本平台事务/guard 契约时，不能替代默认 PortableCheckpointSaver。

### 13.7 SQLite 运行配置

首版数据库位于同一主机的本地持久目录；API、Worker、Scheduler 和 Dispatcher 将配置 URL 解析为同一绝对路径。运行镜像验证 SQLite 链接库 3.51.3+，并在连接建立时设置 foreign_keys=ON、busy_timeout=5000、synchronous=FULL；初始化时确认 journal_mode=WAL、wal_autocheckpoint=1000。[F7][F8][F9]

只允许短写事务；模型、MCP、Git、脚本、对象存储及 SSE 等待都在事务外。每次读批结束即关闭事务。数据库和目录具备持久卷与适当权限，不能把进程临时目录、:memory: 或跨主机共享文件系统作为正式运行后端。

SQLITE_BUSY/LOCKED 由存储层在调用总期限内处理，busy_timeout 和重试不能无限叠加；超过默认 5 秒存储操作期限时返回 STORAGE_BUSY。API 尚未提交时返回 503 并允许同幂等键重试。磁盘满、I/O 错误或完整性检查失败停止写入并取消 ready，不伪造 accepted。

### 13.8 JSON、字节与引用字段契约

TEXT 不表示任意自由结构。新写入先经领域 DTO/协议 Schema 校验，再按 UTF-8、sort_keys、紧凑分隔符、ensure_ascii=False、allow_nan=False 规范化；hash/digest 针对契约规定的字节计算。Skill 目录、文件与检查点有各自编码规则，不能套用一套 JSON 摘要算法。改动 JSON 结构需要版本兼容或显式新版本，不能只因物理列仍为 TEXT 就原地换语义。

#### VersionRef、运行配置与接受快照

| 位置 | 精确键与值类型 | 校验/权威性 |
| --- | --- | --- |
| VersionRef | resource_id:string、version:string、content_hash:64 位小写 SHA-256 | 模型指向 model_version_id，Skill 指向 skill_version_id，工具指向 binding_id，仓库指向 snapshot_id；资源类型由所在字段确定 |
| runtime_profiles.profile_json | default_model:VersionRef、role_models:[{role,ref}]、graph_version:string、skills/tools/repositories:VersionRef[]；可选 description、system_prompt、response_language、response_format、conversation_history_turns、allowed_modes:string[]、budget_limits:{wall_seconds?,model_calls?,tool_calls?,tokens?} | 行为字段类型与默认值见第 20.3 节；role 仅 router/knowledge/diagnosis/code/summary/evaluation 且不重复；发布校验资源引用与授权 |
| runs.versions | team_id、agent_id、subject、identity_kind、agent_scope:string[]或NULL、roles:string[]、accepted_at_ms:int、source:string、agent_profile:VersionRef、graph_version、default_model、role_models、skills、tools、repositories，以及 system_prompt、response_language、response_format、conversation_history_turns 的非默认值；可选 workflow:VersionRef | 接受事务冻结 RunContext；行为字段缺省按领域默认解析；description 不注入执行快照；不能从最新 YAML/Profile 替换，当前撤权即时生效 |
| runs.budgets | wall_seconds:int、model_calls:int、tool_calls:int、tokens:int | 接受时合并默认/Profile/请求限制后的最终正整数上限；wall_seconds 是累计活跃执行预算，不能等同总存活墙钟 |
| query_scope_revisions.scope_json | region?/service/environment/start_at/end_at 等当前领域允许的范围键 | 时间窗为带时区 RFC 3339；区域待确认的初始版可缺 region，完整业务取证必须先解析；变更新增 version，不能直接覆盖 |

`*_versions` 投影与 profile_json 不一致时拒绝使用。当前 JSON 资源引用没有关系型 FK，发布、受理、恢复及每次敏感操作均按 team/agent/resource/version/hash 重新核对；不能据 JSON 内 team_id 声称已授权。

新增行为与上下文字段在取默认值时不写入序列化结果，以保持旧 Profile/Run 快照和内容哈希兼容；读取旧 JSON 时使用领域默认值。不能为“补齐字段”批量改写已发布配置或历史 Run 的 JSON/哈希。

#### ModelProfile 与 ToolBinding

| 存储位置 | JSON 字段 | 边界 |
| --- | --- | --- |
| model_profiles.draft、model_profile_versions.limits | name、provider、adapter、protocol、base_url、model_id、secret_ref、temperature、request_timeout_seconds、max_output_tokens、max_context_tokens、streaming、sdk_max_retries、invocation_max_attempts、structured_output_strategy、provider_options | 首版 provider/adapter=deepseek，sdk_max_retries=0，invocation_max_attempts 为 1–3，structured_output_strategy=json_mode，provider_options={thinking:{type:disabled}}；不保存真实密钥 |
| tool_binding_drafts.payload、tool_bindings.schema | alias、provider、tool_name、server、credential_scope、permission、input_schema、output_schema或NULL、read_only、timeout_seconds、max_output_bytes | 当前 ToolBinding 没有独立 retry_policy JSON 字段；默认 timeout_seconds=10（最大60）、max_output_bytes=65536（范围64–1048576）。重试由工具执行器统一拥有，不能据目标设计假定已落列 |
| tool_bindings.scope/auth_ref | 发布服务生成的范围和认证作用域投影 | schema 中完整绑定、当前可信部署授权和 QueryScope 一起决定是否允许执行；auth_ref 不能带凭据值 |
| model_profile_versions.capabilities | 已记录的模型能力结果 JSON | 配置、发现、实际调用分层标记；探针最新结果也可能在 admin_jobs.result_ref 中，不能仅依据版本行默认为实时健康 |

#### 工作流与巡检载荷

WorkflowDefinition 为 `{name:string, steps:WorkflowStep[]}`，steps 长度 1–32。每个 WorkflowStep 包含：step_id、tool、required（默认 true）、depends_on:string[]、inputs:{目标参数:直接依赖step_id.字段}、arguments:object、condition或NULL、output_schema或NULL、timeout_seconds（1–60，默认20）、failure_policy（stop/continue）。condition 是 `{source:string,equals:JSON标量,negate:bool}`；发布前必须校验 DAG 无环、引用直接依赖、字面参数与映射参数不重叠、输出 Schema 为受限结构。步骤运行结果目前保存在图检查点，不存在独立 workflow_steps/step_runs 物理表；增加并行或步骤审计表须另做迁移。

schedules.payload 包含 owner:AuthContext、spec:ScheduleSpec、workflow_hash:string、workflow_version:int，并可能保存 last_manual_run_id。ScheduleSpec 为 cron、timezone、workflow_version_id、input_text（1–4096字符）、overlap_policy=forbid、query_scope或NULL；query_scope 为 region/service/environment/window_seconds（1–3600秒）。定时查询窗由 due_at 计算，手动触发由实际受理时间计算，不能复用创建 Schedule 的时间。

#### 消息、Outcome 与证据正文

| 位置 | 正文结构 | 不变量 |
| --- | --- | --- |
| messages.content_ref，role=user | CreateRun 请求快照，含 input:{type:text,text,artifact_ids}、mode、business_context 等接受参数 | 入口身份来自认证，正文内身份不参与授权；图当前主要读取本次 input |
| messages.content_ref，role=assistant | {outcome:RunOutcome} | 最终消息与 run_outcomes 同事务产生；草稿走 RunEvent |
| run_outcomes.payload | schema_version=1、kind（foundation.recorded.v1/analysis.v1）、completed:bool、summary、findings、root_cause_hypotheses、impact或NULL、evidence_refs、unresolved_items、proposed_actions、knowledge_gaps、usage_summary | completed=false 必须有未完成项；completed=true 不得有未完成项。每个 finding/hypothesis/impact 引用必须包含在总 evidence_refs 中且属于当前 Run/Scope |
| findings[] | {summary,evidence_refs, status:observed/normal/uncertain} | normal 必须由 quality=valid 的有效证据支持，empty/unavailable 不能证明正常 |
| root_cause_hypotheses[] | {summary,evidence_refs,status:unverified/supported/rejected} | 观察与假设分开；主观置信度不能代替验证 |
| impact | {summary,evidence_refs} | 影响表述需要明确证据来源 |
| proposed_actions[] | {summary,evidence_refs} | 只是建议；真实执行必须另建 Action/Approval/Outbox |
| RunOutcome.usage_summary | status:pending/complete、model_calls、tool_calls、input_tokens、output_tokens、amount_micros、currency | 未知量保持 NULL/pending；详细预留、隔离和按币种结算仍以 usage_* 台账为准 |
| evidence_payloads.payload | 标准工具结果 JSON，或仓库 {commit,path,start,end,sha256,lines:[{line:int,text:string}]} | 仓库行号从 1 开始、start/end 为包含端点的区间；sha256 是归档内完整源文件字节摘要，不是所选行摘要。未注册格式不能自动当作可信证据，truncated 需显式展示 |

RunOutcome 的规范化载荷上限为 2 MiB；大的日志、文件和完整报告引用 Artifact，不直接复制进图状态。evidence.summary 当前可能保存 digest；引用可访问性必须检查正文/对象，而不是检查 summary 非空。

#### 动作、审批与外部回执

actions.payload_ref 为 `{binding:VersionRef,target:{server,credential_scope,tool_name},arguments:object}`。`tool_version = resource_id + ':' + version + ':' + content_hash`；payload_hash 使用 `SHA256(canonical({tool_version,target,arguments}))`。approvals.target 保存相同 target，args_hash 等于 payload_hash；policy_hash 绑定真实部署动作策略，subject 是显式审批人，期限默认 30 分钟。动作提议、审批决定、发送事实必须分别记录。

outbox.payload_ref 必须等于 Action 正文；destination 必须等于固定目标 server。当前 sender 成功回执严格为 `{external_id:string}`，action_id 是下游幂等输入。unknown 只能根据部署所有的可信 lookup 查证；用户提供一个 external_id 或截图不能自动写成已成功。

| tool_invocations.receipt_ref 类型 | 持久字段 | 恢复要求 |
| --- | --- | --- |
| MCP 有效结果 | kind=mcp-evidence-v1、scope_id、binding:VersionRef、evidence_id；业务调用可含 business_scope | 当前权限、Scope、绑定和可访问证据全部匹配才复用 |
| MCP 空结果 | kind=mcp-empty-v1、scope_id、binding；可含 business_scope | 只能还原 empty，不能还原正常结论 |
| 仓库读取 | kind=snapshot-evidence-v1、scope_id、snapshot:VersionRef、evidence_id | 固定 commit/hash、当前 Scope 以及正文未截断均匹配才复用 |
| Runner 终态 | status、exit_code、stdout/stderr、各自 truncated/sha256/bytes | 仅保存每流有界脱敏摘要；不等于脚本产物已经上传可下载 |

usage_reservations.guard_json/usage_quarantine.guard_json 固定原 team_id、thread_id、run_id、job_id、worker_id、run_epoch 等 ExecutionGuard 字段。预留的 request_hash 针对 ReservationRequest，不能与模型提示词摘要混用；模型重试每次预留，未知回执保留独立隔离记录。

#### 管理任务、反馈与策略正文

admin_jobs.payload 按 kind 区分模型版本探针、工具探针、固定 commit 仓库同步、Skill 来源导入、Artifact 暂存上传；类型化 Worker 再次校验 hash、主体、Agent、版本和路径。result_ref 存结果/固定错误 JSON，不返回 secret_ref 对应值。管理 Job 的请求作用域和 Run 不同，不能伪造一个 Run Guard 来领取管理任务。

feedback.payload 当前为 `{subject,rating,comment}`，source_trace 关联 Run；knowledge_gaps 必须引用真实检索轨迹并保留问题分类和 review_status。两表存在不表示后置评估和知识发布工作流已经实现。

action_policy_state.policy_json 由动作授权同步器校验并固定摘要；synthetic_source_policy 保存受控来源 grant，synthetic_proposal_policy 保存单动作规则，synthetic_batch_policy 保存多动作规则列表。合成规则的精确源选择包含 team_id/agent_id/connector_id/identity_id/subject/service/environment/region/rule；多动作项还含 rule_id、scope、binding、target、arguments、approver，每 Scope 最多 4 条规则、总量最多 256 条。策略版本与对应动作的 synthetic_action_policy_bindings 一起核对；这些是本地测试资产，不能直接升级为生产授权。

### 13.9 核心事务与跨表不变量

以下均是存储契约。当前已实现部分与未来真实业务部分必须分别验收，不能以数据库有表替代端到端证明。

| 操作 | 同一短写事务必须完成 | 事务外/重放规则 |
| --- | --- | --- |
| 接受 Run | 当前授权与幂等匹配；必要时创建 Thread；固定 Profile/模型/Skill/工具/仓库/预算；插入 user Message、Run、Job、首事件、usage_budget_policies 和原响应幂等记录 | 提交后才返回202；断线同键查回原 Run；不在事务内执行模型/Git/MCP |
| 领取或接管 | 选定 Job；检查 Thread 活动位置与团队额度；CAS Job；递增 Run epoch；创建/替换 execution_leases；更新 active_executions 和活跃片段 | 默认45秒租约、10秒续期；旧 Worker 不能凭先前 SELECT 的通过结果再写 |
| 写检查点/事件/工具台账 | 同连接校验 Guard、允许状态、当前身份/Scope 后写受保护数据；事件 seq 同事务分配 | 不允许先验租约，再通过另一连接调用原生 Saver；回放只读已提交批次 |
| 停在输入点 | 图中断先保存；协调器幂等投影 interrupts、等待期限、Run waiting 和事件；释放执行租约/团队槽位 | Thread.active_run_id 仍保留；图已存但投影未存时由协调器补齐 |
| 接受用户补充 | 验证 interrupt/revision/过期/主体；插 ResumeRequest；CAS accepted 指针；生成续执行 Job、queued 状态及幂等响应 | 指针冲突全事务回滚；Command 应用的 last_applied_resume_id 先持久化，再继续外部操作 |
| 调用预算/结果 | 发送前原子检查父预算并预留，持久 possibly_sent；返回后以当前 Guard 写 invocation/证据/结算 | 模型/工具网络在事务外；旧 epoch 晚回执进入 usage_quarantine，不覆盖权威结果 |
| 完成分析 | 校验 Outcome/digest/证据 Scope；写最终 assistant Message、run_outcomes、结果指针、终态事件和释放记录；对应业务事件按授权契约同事务写入 | 当前自动 Incident 只覆盖合成来源；目标真实业务投影尚须接入。终态完成不表示外部投递已确认 |
| 批准外部动作 | 重新校验动作版本/目标/参数/政策/审批主体/期限；写审批决定、Action 授权、Outbox 和审计 | 拒绝/过期不产生发送事实；多次审批历史不能覆盖旧回执 |
| 发送及查证 | 先 CAS Outbox 到 sending 并提交；I/O 后凭发送租约 CAS 写 Action/Outbox 终态、回执、关联与投递事件 | 发送和 lookup 均在事务外；发送租约过期或丢响应进入 unknown；重发前须可靠证明未发送 |
| 定时触发 | CAS Schedule revision；插唯一 due_at 的 fire；如可执行，同事务受理独立 Thread/Run 并固定本次查询窗 | 6小时内错过合并最近一次，超窗记 missed；手动记录写 schedule_manual_fires |
| 对象上传完成 | 上传前登记 uploading/Scope；外部对象写入及回读后，当前管理领取/授权/Scope 校验通过才转 available | 对象存储没有与数据库共用的事务；失败不发布下载 URL，孤儿对象单独回收 |

**额外同属检查**

- agents.active_profile_id 必须属于同一 Agent；当前 FK 只约束团队和 Profile 存在。runtime_profiles 中 JSON 版本引用也需核对资源归属。
- messages 的 run_id 必须属于它的 thread_id；evidence.scope_id 必须属于它的 run_id；usage_records.reservation_id 必须对应同 Run 和 invocation。
- approvals.run_id、actions.run_id、Incident/Link/Action 的 Run 必须一致；Action.scope 必须是该 Run 当前 QueryScope。
- runner_cleanup_obligations 的 Thread/Job/Worker/epoch 必须对应创建时的真实执行；独立清理依自己的维护租约，不复用已失效执行权限。
- 逻辑指针与 JSON 没有被物理 FK 覆盖的地方，通过服务检查、同事务写入和恢复校验共同保护；第 13.10 节的加固需新迁移，不能宣称已由现有数据库保证。

### 13.10 待迁移约束、查询索引与后端兼容

以下是明确的后续加固项，本节不执行迁移。先扫描历史重复/越界/不一致数据，形成冲突报告；无法证明的数据不静默去重、截断或补猜。

| 对象 | 后续精确设计 | 当前差异/迁移要求 |
| --- | --- | --- |
| 模型命名 | 增加非空 scope_key:VARCHAR(36)：团队级用固定保留值 `team`，Agent级用 agent_id；UK(team_id,scope_key,name)；校验 scope 与 agent_id 一致 | 当前无名称 UK，不能用 nullable agent_id 的唯一索引假定 NULL 唯一 |
| Skill 命名 | 同样增加 scope_key:VARCHAR(36) 并设 UK(team_id,scope_key,name) | 保留团队共享与 Agent 私有边界，历史同名先人工处理 |
| repo_snapshots.repo_id | 迁移为 VARCHAR(128)，服务按部署仓库键校验；是否同 Agent 的 repo_id 必须与源映射一致 | 当前 VARCHAR(36)，SQLite 未强制长度不能当作其他后端也兼容 |
| active_profile_id | 为 runtime_profiles 加 UK(team_id,agent_id,profile_id)，将 Agent 指针约束为同一 Agent 的复合 FK | 当前仅 FK(team_id,active_profile_id)，服务层仍需检查 Agent |
| 证据与范围 | 为 QueryScope 加 UK(team_id,run_id,scope_id)，Evidence/上传绑定以 team_id+run_id+scope_id 复合引用 | 当前仅分别约束团队；上传 scope_id 无 FK。历史无 Scope 的上传继续显式 NULL |
| 预算结算 | 预留加 UK(team_id,run_id,invocation,reservation_id)，结算以这四列复合 FK 绑定 | 当前 reservation_id FK 只保证同团队。新约束须保留允许空的遗留记录，不伪造预留 |
| 每动作一个发送台账 | 本期一个 Action 对一个目标：验证历史后增加 UK(team_id,action_id) 到 Outbox；重试更新原行 | 当前依赖服务串行检查，尚无该 UK；未来多目标需显式 child Action，不能直接绕开唯一性 |
| 协议元数据 | 新增 singleton_id:VARCHAR(16) PK，CHECK singleton_id='storage' | 当前由 ready 要求恰一行；多行必须报错后维护修复，不能取第一行继续 |
| Run 终态时间 | 新增 finished_at:BIGINT NULL，终态事务显式写入；新增非负/0或1 CHECK 仅在清理历史后施加 | 当前以保守 updated_at 延长保留，不估算旧片段时长或根据信息不足的旧事件填时间 |

**查询索引计划**

第 13.2 节列出的显式索引才是现状；下表是按访问模式新增的候选。实现阶段在真实数据量下用 EXPLAIN 和容量测试决定是否落库，避免把所有候选无条件叠加到 SQLite 写路径。

| 访问模式 | 候选索引列顺序 | 查询约定 |
| --- | --- | --- |
| 工作台任务列表 | runs(team_id,agent_id,created_at,run_id) | 按创建时间和 ID 稳定游标分页，禁止大 OFFSET |
| Agent 状态筛选 | runs(team_id,agent_id,status,created_at,run_id) | 身份范围先过滤，不依赖 JSON 查询 |
| 会话消息分页 | messages(team_id,thread_id,created_at,message_id) | 同毫秒使用 message_id 破除并列；当前索引仅 thread_id/created_at |
| Run 的范围/证据 | query_scope_revisions(team_id,run_id,version)；evidence(team_id,run_id,scope_id,created_at,evidence_id) | 当前范围显式版本排序，证据先按当前 Scope 筛选 |
| 事件批量过期 | run_events(occurred_at,run_id,seq) | 有界批次并推进 event_floor；不可占用长写事务 |
| 待执行定时配置 | schedules(status,next_due_at,schedule_id) | UTC 到期扫描，单项 revision CAS |
| 待清理资源 | runner_cleanup_obligations(daemon_id,status,next_retry_at,invocation_id) | 清理前另验原执行租约失活 |
| 团队用量明细 | usage_records(team_id,created_at,run_id,record_id) | 按币种及 pending 状态聚合，不从 JSON 成本猜值 |
| 动作审批历史 | approvals(team_id,action_id,created_at,approval_id) | 当前审批选择规则需稳定排序；外部回执不可被审批覆盖 |
| 管理任务详情 | admin_jobs(team_id,agent_id,created_at,job_id) | 探针结果不能越 Agent 披露 |
| 知识与反馈审核 | knowledge_gaps(team_id,review_status,created_at,gap_id)；feedback(team_id,run_id,created_at,feedback_id) | 待审与已审分开，分页保留来源 |

MySQL 的复合索引按实际字符集和目标 InnoDB 索引限制核算，机器键采用二进制比较。当前 Agent 幂等 UK(team_id,subject,agent_id,route,key) 的声明长度为 36+128+36+128+128=456 字符；按 utf8mb4 最多 4 字节估算，键列为 1824 字节，在 3072 字节上限下可以保留全长，不能无依据改为前缀唯一索引。checkpoint namespace 已采用固定摘要压缩长键。未来扩大字段或引入更长身份时，才通过新迁移增加完整作用域摘要键，并始终保留原文碰撞检查；不得截断业务键来通过索引创建。

### 13.11 旧系统导入与切流的规划表

以下两表对应第 21 节与 M4 资产导入/唯一引擎路由，**当前 metadata 和迁移中不存在**。字段是后续实现契约，不属于上面 61 张当前应用表；必须使用新的迁移创建，不能伪装成已完成迁移。

#### `migration_routes`：一个来源事件的唯一主动引擎

| 字段 | 类型 | NULL | 默认 | 含义 |
| --- | --- | --- | --- | --- |
| team_id | VARCHAR(36) | 否 | — | 团队 |
| route_id | VARCHAR(36) | 否 | — | 路由记录 UUID，PK |
| connector_id | VARCHAR(128) | 否 | — | 唯一接入连接器 |
| external_event_id | VARCHAR(128) | 否 | — | 外部去重事件身份 |
| request_hash | VARCHAR(64) | 否 | — | 原规范化来源载荷 SHA-256 |
| engine | VARCHAR(32) | 否 | — | legacy/langgraph，首次分配后不可因 Profile 更新重选 |
| routing_policy_version | VARCHAR(128) | 否 | — | 首次分配使用的路由策略版本 |
| run_id | VARCHAR(36) | 是 | — | 新引擎 Run；旧引擎路径为空 |
| legacy_task_ref | TEXT | 是 | — | 旧引擎任务引用；不伪造新 Run/checkpoint |
| notification_owner | VARCHAR(32) | 否 | — | 唯一允许通知的引擎，必须等于主动 engine |
| status | VARCHAR(32) | 否 | — | reserved/dispatched/terminal；reserved 恢复仍使用原 engine |
| revision | BIGINT | 否 | 0 | 路由投影 CAS 版本 |
| created_at | BIGINT | 否 | — | 创建 UTC 毫秒 |
| updated_at | BIGINT | 否 | — | 更新 UTC 毫秒 |

约束：PK(route_id)，UK(team_id,route_id)，UK(team_id,connector_id,external_event_id)，FK(team_id)→teams、FK(team_id,run_id)→runs，均 RESTRICT；索引(status,updated_at,route_id) 用于中断派发修复。服务保证请求摘要一致、engine/notification_owner 固定，langgraph 只填 run_id、legacy 只填 legacy_task_ref，reserved 阶段允许尚未填任务引用。跨进程派发采用下游幂等请求键和持久状态恢复；数据库事务不包裹旧引擎网络调用。

新旧系统必须共用这个接入/通知权威，分别订阅同一事件后各自去重不满足唯一主动执行。影子 Run 记录独立成本，Tool 层与 Dispatcher 双重禁止写操作和通知，不能抢占此行的主动引擎归属。

#### `import_records`：可审计资产导入

| 字段 | 类型 | NULL | 默认 | 含义 |
| --- | --- | --- | --- | --- |
| team_id | VARCHAR(36) | 否 | — | 目标团队 |
| record_id | VARCHAR(36) | 否 | — | 导入记录 UUID，PK |
| agent_id | VARCHAR(36) | 是 | — | 目标 Agent；团队级资产可空 |
| manifest_hash | VARCHAR(64) | 否 | — | 已审核导入清单 SHA-256 |
| source_engine | VARCHAR(32) | 否 | — | ai-planner/ai-agents 等已注册来源 |
| source_type | VARCHAR(32) | 否 | — | 历史来源类型 workspace/skill/tool/repository/incident/history/cost；workspace 仅标识导入来源，目标归属使用 agent_id |
| source_id | TEXT | 否 | — | 来源完整身份，不含秘密 |
| source_version | VARCHAR(128) | 否 | — | 来源版本；未版本化来源须用经审核快照版本 |
| source_key_hash | VARCHAR(64) | 否 | — | SHA256(canonical({source_type,source_id,source_version})) |
| content_hash | VARCHAR(64) | 否 | — | 实际转换输入内容摘要 |
| target_type | VARCHAR(32) | 否 | — | 已注册目标资源类型 |
| target_id | VARCHAR(128) | 是 | — | 成功导入的目标资源 ID |
| target_version | VARCHAR(128) | 是 | — | 成功导入的目标版本 |
| status | VARCHAR(32) | 否 | — | preview/imported/failed |
| result_json | TEXT | 否 | — | 字段映射、校验和结果/固定错误 JSON |
| created_by | VARCHAR(128) | 否 | — | 审核后执行导入的认证主体 |
| created_at | BIGINT | 否 | — | 创建 UTC 毫秒 |
| updated_at | BIGINT | 否 | — | 更新 UTC 毫秒 |

约束：PK(record_id)，UK(team_id,record_id)，UK(team_id,source_engine,source_key_hash)，FK(team_id)→teams、FK(team_id,agent_id)→agents，均 RESTRICT；索引(team_id,manifest_hash,status,record_id)。同来源身份/版本重复导入且内容一致重放目标引用，内容不同必须冲突；摘要键仍核对 source_type/source_id/source_version 全文。target_type/id 是多态逻辑引用，必须由已注册导入器验证，不能宣称数据库 FK 已保护任意目标类型。

preview 不修改正式资源，apply 只接受已审核的确切 manifest；正式资源写入与 imported 结果在同事务完成，对象写入失败不发布引用。历史 SDK 会话只导入来源明确的消息/摘要，新建 Run，不生成伪造的 LangGraph checkpoint；历史费用保留来源命名空间，不能与新调用去重身份混用。

### 13.12 Schema 演进、对象引用与恢复校验

**迁移元数据与变更纪律**

除应用表外，Alembic 管理 `alembic_version`：version_num 为 VARCHAR(32)、非空主键、无默认值；无分支迁移时恰一行，值须匹配新源码声明的迁移头。重构前的 `0021_synthetic_batch_policy` 及 59 表/575 字段只作历史参照。ready 检查迁移头、协议唯一行、表/列类型与空值、PK/UK/索引/FK、PRAGMA 及完整性；并未逐项比较所有 server_default 或 CHECK 表达式，不能把 ready 当作完整 DDL 等价证明。

后续兼容性演进使用新的 Alembic revision 和不可变 schema_vN 快照，升级 schema.py 导出。本次 Agent 破坏性基线仅用于显式选择的新 SQLite 路径；旧 Workspace 库必须在迁移和 readiness 阶段安全拒绝，给出保留旧库、选择新路径及显式初始化的操作指引，不能自动删库、清表、stamp 或原地假升级。普通 API/Worker/Scheduler 不执行 create_all/自动迁移。其他数据迁移须先备份并按维护窗口执行；不能假设各后端都支持延迟 FK。

**对象引用和可用性**

| 资源 | 数据库权威引用 | 当前格式/验收要求 |
| --- | --- | --- |
| Skill 目录 | skill_versions.bundle_ref/hash | 当前清单为 skill-manifest-v1:sha256:<manifest摘要>，含 format/name/description/hashes；目录 hash 来自规范化 path→资源hash 映射，单资源读取再校验字节摘要 |
| 固定仓库 | repo_snapshots.storage_ref/commit_sha 与 Run VersionRef.content_hash | 当前本地内容寻址归档，不是直接 git pull 的运行目录；同 Run 固定提交和归档摘要 |
| 大证据/报告 | artifacts.object_key/sha256/size/status 和 evidence.artifact_ref | 文件上传完成、回读校验后才 available；下载先当前授权再签短时 HTTPS URL，签名器缺失明确不可用 |
| 有界证据正文 | evidence_payloads.payload/truncated | 当前允许内联有界正文；超限转对象或明确截断，不能把截断正文当完整事实 |
| 上传暂存 | admin_jobs 中受控暂存身份及 artifact_upload_scopes | 不是正式对象；租约/当前权限/Scope 校验失败不得标 available，孤儿暂存独立清理 |

当前 Skill/仓库主要使用本地内容寻址目录，Artifact 上传可配置 S3；不能据目标对象存储架构宣称所有资源都已走 S3。Artifact/Skill/快照引用与数据库应一起纳入恢复清单，临时路径、预签 URL 和凭据值不进入逻辑迁移包。

**恢复与跨后端转移的最小校验项**

- Online Backup 恢复先隔离写入，验证 SQLite integrity_check/foreign_key_check、当前迁移头、协议行和逐表行数/摘要；源库备份时仍可前进，不以复制单个活跃 WAL 主文件代替一致备份。
- 保留资源 ID、team/agent 作用域、Run epoch、Job generation、seq/event_floor、固定版本/hash、accepted_resume_request_id，以及完整 checkpoint 父链、namespace 原文、typed bytes 和 pending writes。
- 验证 active_run_id、审批/Outbox/外部关联、证据 Scope 和对象引用没有悬空；执行旧租约统一失效，由新 epoch 接管，不能恢复旧 owner 继续写。
- 对所有 available Artifact 和发布资源核对对象存在、长度/摘要；对 sending/unknown Action 进行真实下游查证，不把数据库快照状态当作外部发送事实。
- 当前 acknowledge-restore 只放行没有外部 Artifact/Action/Outbox 的干净 M1 副本；带外部效果的完整对账放行流程仍未实现，必须保持隔离，不能通过本次文档解除。
- MySQL/PostgreSQL 适配及 export/import/validate 工具属于 M5；通过相同 Repository/Job/Guard/Saver/预算契约、索引兼容及恢复测试后才开放，不以更换 URL 当作迁移完成。

本节字段基线来源为 [当前 Schema 导出](../../apps/agent/src/workbench_agent/persistence/schema.py)、[Agent Schema](../../apps/agent/src/workbench_agent/persistence/schema_agent_v3.py)、[Agent 迁移目录](../../apps/agent/src/workbench_agent/persistence/agent_migrations/versions/)、[RunContext](../../apps/agent/src/workbench_agent/domain/context.py)、[Profile](../../apps/agent/src/workbench_agent/domain/profiles.py)、[Workflow](../../apps/agent/src/workbench_agent/domain/workflows.py)和对应 Store；[历史基础表](../../apps/agent/src/workbench_agent/persistence/schema_v1/__init__.py)及 schema_v2–schema_v20 仅作旧基线参照。未来实现更改这些事实时，应同步本字典和验收记录。

## 14 状态机

### 14.1 Run 状态机

![Run 状态机](../assets/workbench-python/07-run-state.png)

[SVG 图](../assets/workbench-python/07-run-state.svg) · [Mermaid 图源](../assets/workbench-python/07-run-state.mmd)

| 当前状态 | 事件和条件 | 下一状态 |
| --- | --- | --- |
| queued | Worker 成功领取，Thread 活动槽位可用 | running |
| queued | 取消成功 / 墙钟过期 | cancelled / expired |
| running | 图产生已持久化中断 | waiting_input / waiting_approval |
| waiting_input / waiting_approval | 有效恢复请求入库并创建 Job | queued |
| waiting_input / waiting_approval | 取消 / 等待或墙钟过期 | cancelled / expired |
| running | 完整或部分 Outcome 已保存到检查点 | finalizing |
| running | 明确不可恢复错误 | failed |
| running | 活跃执行硬超时且未形成有效 Outcome | timed_out |
| running | 接受取消 | cancel_requested |
| cancel_requested | 执行停止、租约释放或可安全回收 | cancelled |
| running / finalizing | 可恢复故障，执行锁已释放，未超过尝试上限 | queued，携带 recovery_phase |
| finalizing | 业务结果事务提交 | succeeded / partial |
| finalizing | 不可恢复的结果校验错误 | failed |

succeeded、partial、failed、cancelled、timed_out、expired 为终态。数据库更新使用状态条件、revision 和 run_epoch，禁止旧 Worker 把终态覆盖为运行中。finalizing 表示模型分析已经结束，恢复该阶段只提交已有结果，不重新分析。

预算检查节点在硬截止前已经生成带未完成项的有效结论时，finalize 为 partial；硬截止强行终止执行且没有有效 Outcome 时为 timed_out。expired 用于排队、等待及总墙钟期限，不混同工具 timeout。

### 14.2 Interrupt 与 ResumeRequest

Interrupt 状态是 open、response_submitted、consumed、expired、cancelled。ResumeRequest 是 submitted、applying、applied、rejected。恢复失败但未应用时保留 submitted/applying 并由协调器判断检查点；不能释放同一等待点让第二条输入覆盖第一条。

拒绝动作确认也是有效恢复输入，图据此生成跳过/解释结果。它不会令整个 Run 自动失败。

### 14.3 外部动作状态机

![外部动作状态机](../assets/workbench-python/09-action-state.png)

[SVG 图](../assets/workbench-python/09-action-state.svg) · [Mermaid 图源](../assets/workbench-python/09-action-state.mmd)

Action 状态是 planned、waiting_approval、authorized、dispatching、retry_wait、succeeded、failed、unknown、rejected、cancelled、expired。

- planned 按策略直接进入 authorized，或进入 waiting_approval。
- 有效批准进入 authorized；拒绝、过期、取消分别进入对应终态。
- Dispatcher 在短事务内领取 authorized/retry_wait 动作并进入 dispatching；执行前发现权限已撤销时转 failed，错误码 OPERATION_DENIED，相应 Outbox 转 dead。
- 明确成功进入 succeeded，明确可重试失败进入 retry_wait，永久失败进入 failed。
- 已发送请求但未能确认结果进入 unknown，执行结果查询或人工对账；不能自动当成 failed 盲目重发。
- unknown 经可靠回执或查询进入 succeeded/failed；没有可核对事实时保持 unknown。

### 14.4 Job 状态

当前 SQLite Job 状态为 ready、leased、retry_wait、done、dead、capability_blocked，领取在短写事务中直接进入 leased；适配器若采用两阶段候选预留，可设计短暂的 claiming 状态，但必须在同一事务完成执行租约和 epoch 更新后才允许执行。执行失败按策略进入 retry_wait 或 dead。缺少所需能力时进入 capability_blocked，之后扫描会重新检查能力，满足时可直接领取为 leased。

Job 只是调度状态，不能据此单独推断 Run 已完成；图已暂停或终结后，相应 Job 可以标记 done。等待用户的 Run 没有持续 leased 的 Job。

### 14.5 投递状态

Outbox 状态为 pending、sending、retry_wait、delivered、dead、unknown。Run 终态与投递状态独立。死信可人工重试，但保留原 action_id 和业务幂等键；更新投递回执只追加 RunEvent，不改变分析终态。

## 15 调度并发与一致性

### 15.1 原子 Job 领取

Worker 只领取自己支持 Graph 版本且具备依赖能力的 Job。JobStore.claim 在一个短 UnitOfWork 中完成候选复核、团队额度、Thread 活动位置、ExecutionLease、Run epoch 和 Job owner 的更新；任一条件失败回滚，不能只成功更新其中一张表。

SQLite 首版以 BEGIN IMMEDIATE 取得写事务，重新读取已到期候选和 active_run_id，以状态/版本条件更新并核对影响行数，然后一次提交。MySQL/PostgreSQL 的适配器在 READ COMMITTED 下使用一致顺序的行锁或 CAS，SKIP LOCKED 仅作为减少等待的优化。业务服务不写后端专属 SQL。

多对象更新的顺序固定为团队额度 → Thread → ExecutionLease → Run → Job → Interrupt → Action/Outbox；只涉及其中部分对象时保持相同相对顺序。epoch 只在成功领取/接管的同一事务内增加。没有领取所有权的候选扫描不能使旧执行失效。

SQLite P0 默认一个 Worker 实例、2 个 Run 并发、团队最多 2 个活跃执行；最多 2 个专家和 4 个工具并发，但其检查点/台账写入仍是短事务串行竞争。等待用户释放活动执行额度，继续占业务 Thread 的逻辑活动位置。后续数据库配置更高并发前需经过契约与容量验收。

### 15.2 事务内租约与 fencing

Job/ExecutionLease 租约为 45 秒，每 10 秒续期；扫描器每 5 秒发现到期候选。ExecutionLease 持久化 thread_id、owner_run_id、owner_job_id、worker_id、run_epoch、expires_at 和 guard_revision。续期在同一事务中更新 Job 与 ExecutionLease，失败时不能继续派发新操作。

接管以条件更新取得已过期或显式释放的执行租约，并在同一事务内增加 Run epoch。旧 Worker 的网络请求可能仍在返回，但它不能再提交属于旧所有者的状态。

每次检查点、pending write、执行事件、预算结算及业务结果写入都使用 LeaseGuard：

1. 在写入使用的 UnitOfWork 中，对 ExecutionLease 做条件更新并增加 guard_revision，条件包括 owner/run/job/epoch 相等且未过期；此更新使同一租约的接管与写入在数据库中串行化。
2. 校验当前 Run 状态和该操作允许的取消状态，再写入检查点或业务台账。
3. 所有变更在同一事务提交。条件更新影响行数不是 1 时，回滚并抛出 STALE_EXECUTION，不进入通用重试。

SQLite 由单写事务串行化 Guard；MySQL/PostgreSQL 由同一租约行的条件更新锁串行化。正确性不依赖 PostgreSQL advisory lock、长连接身份或 Redis 锁。数据库连接可以按池策略回收，但一次 Guard 与写入不能跨连接/跨事务。

若租约续期失败或后端不可用，Worker 停止派发新操作并协作取消当前尝试；失效结果只进入独立尝试审计。外部副作用仍通过 Action 的稳定标识与对账处理，fencing 不承诺撤销已发送的网络请求。

### 15.3 检查点与业务投影

Guard 校验和单次检查点写入原子提交，但图状态与后续业务投影仍可能处于两个事务。协调器使用图元数据、interrupt 和 outcome 幂等投影：

| 检查点 | 业务状态 | 协调动作 |
| --- | --- | --- |
| 有 pending interrupt | running 或 queued | 补齐 Interrupt、waiting 状态和事件 |
| last_applied_resume_id 已存在 | ResumeRequest applying | 标记 applied，不再次发送相同恢复命令 |
| 有有效 Outcome / 图结束 | running 或 queued | 切 finalizing，只执行 finalize |
| 有可继续 next 节点 | 非终态且执行租约可被原子领取 | 创建 recover Job，从最新检查点恢复 |
| Run 已终态 | 有重复 Job | 结束 Job，不重新运行图 |
| 状态或版本无法安全解释 | 非终态 | failed，错误码 STATE_RECONCILIATION_REQUIRED，保留诊断材料 |

finalize 在一个业务事务中保存 Outcome、Duty Event、Outbox、Run 终态和最终 RunEvent。稳定结果键及摘要使其可重复调用。检查点已完成但业务结果未提交时，可以重新 finalize。

RunEventStore 区分执行写入、控制命令和交付回执三类写入权限。执行事件校验活动 LeaseGuard；取消/恢复由授权控制命令事务写入；Run 终态后的 Action/Delivery 回执由其动作租约校验后追加。所有路径在同一事务内更新 runs.last_event_seq 并插入事件，不能依赖数据库自增序列。

### 15.4 调度与恢复公平性

Job 优先级依次为恢复/人工回复、在线交互、告警、定时任务；同优先级按团队轮转。等待 Thread 的候选延迟 1 秒重查，不占 Worker 执行槽位。

一个执行片段最多 3 次基础设施尝试，失败后退避 2 秒、10 秒并加抖动；人工恢复开启新片段但继续消耗同一个 Run 总预算。无支持版本的 Job 标记 capability_blocked 并触发运维事件，发布期间保留对应版本能力。

SQLITE_BUSY、服务型数据库死锁和串行化失败由 UnitOfWork 在剩余期限内进行数据库事务重试；重试函数只能包含数据库操作，不重复模型、工具或外部动作。不能与连接器/Job 重试叠加成无界尝试。

## 16 容错与恢复

### 16.1 故障恢复时序

![故障恢复与对账时序](../assets/workbench-python/06-recovery.png)

[SVG 图](../assets/workbench-python/06-recovery.svg) · [Mermaid 图源](../assets/workbench-python/06-recovery.mmd)

### 16.2 故障处理矩阵

| 故障位置 | 检测 | 处理和状态 | 重试边界 |
| --- | --- | --- | --- |
| API 提交前失败 | 数据库事务失败 | 不返回 202 | 客户端同幂等键重试 |
| 已提交但响应丢失 | 幂等记录 | 返回原响应 | 不重复创建 Run |
| Redis 不可用/通知丢失 | 健康与轮询 | 数据库轮询，限流回落数据库 | 不丢已受理 Job |
| Worker 被终止 | 租约过期与锁释放 | 协调后新 epoch 恢复 | 同一片段最多 3 次 |
| 数据库连接断开 | 后端统一 STORAGE_UNAVAILABLE | 停止派发和当前尝试写入 | 后端恢复后按租约重新领取 |
| SQLite 写忙 | SQLITE_BUSY/LOCKED | 在 5 秒存储期限内等待，超期返回 STORAGE_BUSY | 只重试纯数据库短事务 |
| 旧 Worker 在接管后晚写 | LeaseGuard 所有者/epoch 不匹配 | 回滚并返回 STALE_EXECUTION | 不自动重试旧所有权写入 |
| 模型 429/瞬时 5xx | 协议错误分类 | 遵循 Retry-After，受截止时间与预算限制 | 单模型调用最多 3 次总尝试 |
| 模型 Schema 不合法 | Pydantic 验证 | 一次修正，仍失败则 partial/failed | 不无限自动修正 |
| 工具读取超时 | 调用 deadline | 记录 unavailable/timeout，必要时换证据或人工 | 读操作最多 3 次总尝试，退避 0.5/2 秒 |
| 工具权限或参数错误 | 工具层验证 | denied/invalid_input，不当作正常 | 不自动重试相同请求 |
| 外部写入响应丢失 | invocation 已发送但无回执 | Action unknown，查询结果或对账 | 不盲重试非幂等写入 |
| SSE 断线 | 客户端重连 | 按 seq 回放 | 不取消图执行 |
| 早先文本在恢复后被重新生成 | attempt 不同 | 标记旧草稿 superseded，最终答案独立 | 允许重算，费用照实记录 |
| 中断已入检查点但投影丢失 | 协调器 | 重建 waiting 投影 | 不再次调用模型追问 |
| 用户重复/并发回复 | 幂等与等待点约束 | 原响应或 409 | 单中断一条有效响应 |
| 预算或硬期限到达 | 统一预算服务 | partial 或 timed_out | 不通过重试刷新预算 |
| Runner 容器失败 | 心跳/退出码 | 保留已有产物，安全读/脚本步骤按策略重试 | 非幂等脚本需对账 |
| 产物上传中断 | Artifact 状态和摘要 | uploading 不可被引用 | 幂等上传后标 available |
| Jira/通知下游失败 | Dispatcher | pending/retry_wait/dead，分析结果保留 | 见投递策略 |
| Graph/Skill/快照版本缺失 | ContextResolver | 暂停派发并报 capability/version 错误 | 不静默换新版本 |
| 用户凭据失效 | 连接器认证错误 | 等待授权或输出不可用项 | 重新授权后需服务端重新绑定作用域 |

框架检查点在执行边界保存状态，失败时可能重新执行节点；外部副作用并不由检查点事务保护。[F5]

### 16.3 重试所有权

连接器负责网络级瞬时错误重试，图节点不再叠加相同重试；Job 级重试处理 Worker/数据库等基础设施故障。每次真实尝试记录 attempt 并消耗预算，避免工具 3 次 × 节点 3 次 × Job 3 次的乘法重试。

Outbox 最多 8 次投递，总尝试间隔依次为 2 秒、10 秒、30 秒、2 分钟、10 分钟、30 分钟、2 小时。永久权限/参数错误直接进入 dead；不确定是否生效的请求进入 unknown。用户可重试已确认失败的投递，但不能跳过 unknown 对账。

### 16.4 已发送但未确认的外部操作

动作的 action_id 和规范化参数哈希必须在请求发出前提交。下游支持幂等键时重用该键；不支持时根据外部关联字段查询。对账无法确定时保留 unknown 并通知有权限的值班人。

这提供可恢复、可去重及可对账的处理，不承诺所有外部系统上的天然恰好一次执行。外部动作确认和重复通知测试是独立验收项。

### 16.5 灾备边界

应用进程重启情况下，当前数据库已持久化确认的任务和事件不依赖本机内存恢复。数据库故障切换和介质损坏的数据恢复能力取决于同步复制、备份及对象存储策略，不由 LangGraph 单独保证。

SQLite 首版使用 Online Backup API 或 VACUUM INTO 生成一致快照，恢复前执行完整性检查；不得在运行中仅复制 .sqlite3 主文件而忽略 WAL 状态。[F11] 后续 MySQL/PostgreSQL 按各自的一致备份、binlog/WAL 和复制策略部署，数据库 WAL 与 LangGraph 检查点是不同概念。对象版本管理和恢复演练属于部署要求。备份恢复后先停止外部写动作，协调检查点/Run/Action 台账并对账，再开放 Dispatcher。不能从旧备份恢复后直接重发所有历史 Outbox。

## 17 权限与操作确认

### 17.1 权限交集

有效工具权限是用户权限、团队策略、Agent 绑定、场景/Skill 允许范围和专家允许范围的交集。模型只提出操作，工具执行层与下游系统分别校验。Skill 的说明和工具可见列表不替代执行授权。

P0 角色为 viewer、operator、maintainer、admin。viewer 查看已授权团队的结果；operator 创建与恢复 Run；maintainer 管理 Skill/Agent 草稿，发布权限以具体用例为准，其中团队共享 Skill 发布要求 admin；admin 管理成员、连接和策略。业务工具仍检查独立的下游权限。

### 17.2 确认契约

确认请求绑定 action_id、tool_version、目标、参数哈希、策略版本、允许确认的主体与 expires_at，默认 30 分钟。decision 必须来自授权用户的显式提交。批准后执行前重新检查当前拒绝规则，避免批准期间权限已撤销。

多个待确认动作分别有 approval_id，不接受“对最近一条全部同意”作为不具名恢复。审批人修改参数后生成新摘要和新确认，不默默沿用原批准。

### 17.3 数据与执行权限

API、SSE、产物下载、Resume、Cancel 和动作对账使用同一资源授权边界。Runner 的文件与网络策略约束本地执行；MCP 和业务连接器的远端访问由各自认证与执行层校验。

Webhook 校验平台签名、时间戳和来源事件去重。通知目标来自已绑定团队渠道或获授权的明确目标，避免由模型输出任意回调地址。日志、SSE、Trace 和模型输入只包含任务所需数据，并在进入外部观测系统前脱敏。


## 18 配置与运行契约

### 18.1 配置层次

配置分为四层：部署配置管理依赖、身份、凭据授权与进程参数；ModelProfile 管理端点、模型、温度、上下文/输出上限及超时；Agent RuntimeProfile 管理工作流、行为、上下文策略、预算及资源版本引用；Run 快照记录受理时实际生效的版本和限制。模型输出、用户正文、系统提示词或 Skill 不能直接修改部署配置。

Agent 保存草稿后须显式发布，新 Run 才生效，无需为单纯 Profile 发布重启服务。模型新版本也须在 Agent 中重新选择并发布，不会自动替换旧绑定。修改 Python 代码需重启相关进程；修改前端需重新构建并按部署方式更新静态资源；修改环境密钥或部署凭据授权需重启消费它们的进程。

**新建 Agent 不等于获得模型使用授权。** 部署管理员需在 `service.model_credential_bindings` 中通过 `agent_ids` 授权允许的 Agent 和模型端点。环境变量密钥还须匹配该绑定的 secret_ref；管理页面可安全录入加密密钥，但不能代替部署授权，具体启动和备份步骤见[本地启动手册](../runbooks/source-layout.md#管理端模型密钥)。连接器白名单也使用 `agent_ids`，资源绑定使用单数 `agent_id`，身份限制使用 `agent_scope`。缺失授权或模型不可用时应保留明确错误，不以基础固定响应伪装模型成功。

~~~yaml
service:
  name: workbench-agents
  default_timezone: Asia/Shanghai
  auth_mode: oidc
  oidc_issuer_ref: env://WORKBENCH_OIDC_ISSUER
  oidc_audience: workbench-agents
llm:
  bootstrap_policy: create_if_missing
  default_profile: deepseek-dod-default
  profiles:
    - name: deepseek-dod-default
      provider: deepseek
      adapter: deepseek
      protocol: openai_chat_completions
      base_url: https://api.deepseek.com
      model_id: deepseek-flash
      secret_ref: env://DEEPSEEK_API_KEY
      temperature: 0
      request_timeout_seconds: 60
      max_output_tokens: 4096
      max_context_tokens: 64000
      streaming: true
      sdk_max_retries: 0
      invocation_max_attempts: 3
      structured_output_strategy: json_mode
      provider_options:
        thinking:
          type: disabled
storage:
  backend: sqlite
  database_url_ref: env://WORKBENCH_DATABASE_URL
  schema_version: dod-storage-agent-v1
  checkpoint_store: portable
  checkpoint_codec_version: dod-checkpoint-v1
  sqlite:
    journal_mode: WAL
    synchronous: FULL
    foreign_keys: true
    busy_timeout_ms: 5000
    wal_autocheckpoint_pages: 1000
  object_endpoint_ref: env://WORKBENCH_S3_ENDPOINT
  object_bucket: dod-artifacts
  object_auth_ref: secret://dod/object-storage
redis:
  url_ref: env://WORKBENCH_REDIS_URL
  required_for_admission: false
jobs:
  backend: database
  poll_interval_ms: 1000
  lease_seconds: 45
  heartbeat_seconds: 10
  max_segment_attempts: 3
worker:
  concurrency: 2
  supported_graph_versions: [dod.v1]
  expert_concurrency_per_run: 2
  tool_concurrency_per_run: 4
stream:
  flush_interval_ms: 250
  flush_max_chars: 256
  heartbeat_seconds: 15
  event_retention_days: 7
  client_buffer_events: 100
runner:
  image_profile: python312-tools-v1
  script_timeout_seconds: 60
  cpu_limit: 2
  memory_mib: 2048
  max_artifact_mib: 20
~~~

llm 段是 ModelProfile 初始化种子，首次发布版本为 deepseek-dod-default@1。bootstrap 为新 Agent 设置 default_model_profile_ref，角色未显式绑定时全部继承该版本。DEEPSEEK_API_KEY 通过运行环境或受控秘密注入设置；配置文件仅包含引用，不能把真实 Key 写入文档或提交到仓库。

image_profile 由管理员映射到已发布镜像 digest。示例只包含配置契约和秘密引用，实际值由部署环境提供。SQLite URL 解析为同一绝对本地路径，并检查链接库、持久卷、PRAGMA 和目录权限；服务型后端检查数据库版本、事务隔离和连接参数。backend 与 URL 驱动不匹配、检查点使用不同数据库或不支持所选能力时拒绝 ready。

### 18.2 本地启动命令契约

以下命令以 `/Users/xianguiwang/Projects/workbench` 为工作目录，使用已实现的 Agent CLI。SQLite 需要 Python 实际链接 SQLite 3.51.3 或更高版本；配置中的凭据引用需由执行环境按需注入。完整环境设置与故障恢复见 [本地启动手册](../runbooks/source-layout.md) 和 [SQLite 运维手册](../runbooks/sqlite-recovery.md)。

~~~bash
export WORKBENCH_ROOT="$PWD"
export WORKBENCH_ASSET_ROOT="$WORKBENCH_ROOT"
export WORKBENCH_FRONTEND_DIST_ROOT="$WORKBENCH_ROOT/apps/web/dist"
mkdir -p "$WORKBENCH_ROOT/var"
export WORKBENCH_PERSISTENT_ROOT="$WORKBENCH_ROOT/var"
export WORKBENCH_DATABASE_URL="sqlite+aiosqlite:///$WORKBENCH_ROOT/var/dod.sqlite3"
export WORKBENCH_SKILL_OBJECT_ROOT="$WORKBENCH_PERSISTENT_ROOT/skill-objects"
mkdir -p "$WORKBENCH_SKILL_OBJECT_ROOT"

cd "$WORKBENCH_ROOT/apps/web"
npm ci
npm run build

cd "$WORKBENCH_ROOT/apps/agent"
uv sync --frozen --group dev
# 仅首次初始化或明确维护时执行；禁止普通进程启动时自动迁移/重建
uv run workbench db migrate --config "$WORKBENCH_ROOT/config/local/agent.yaml"
uv run workbench bootstrap --config "$WORKBENCH_ROOT/config/local/agent.yaml"
uv run workbench doctor --config "$WORKBENCH_ROOT/config/local/agent.yaml"
~~~

`doctor` 通过后，在分别设置相同环境变量的三个终端，从 `apps/agent` 运行：

~~~bash
uv run workbench api --config "$WORKBENCH_ROOT/config/local/agent.yaml" --host 127.0.0.1 --port 8000
uv run workbench worker --config "$WORKBENCH_ROOT/config/local/agent.yaml"
uv run workbench scheduler --config "$WORKBENCH_ROOT/config/local/agent.yaml"
~~~

页面地址是 `http://127.0.0.1:8000/app/`。MCP Server 从 `apps/mcp-server` 使用 `uv run workbench-mcp-server check|serve --config <绝对配置路径>` 单独启动；先根据模板提供有效数据源、主体权限和密钥引用。`dod runner check` 用于检查 Runner，当前没有 `dod dispatcher` 或 `dod runner` 独立服务启动命令。项目 Compose 定义 API、Worker、Scheduler 和可选 MCP profile，不提供 `minio` 或 `redis` 服务。Compose 配置和镜像已准备，仍须在具备 Docker 的环境完成构建、运行和 TLS 代理验收。

本地演示使用明确标记的 development 身份模式；生产 ready 检查必须拒绝 development 身份配置和非持久化 SQLite 路径。各组件的锁文件、迁移版本、镜像 digest 和 Graph 注册版本属于可重现部署的一部分。

### 18.3 生产发布

1. 在隔离作业中迁移兼容 Schema，备份并记录版本。
2. 启动支持新 Graph 版本的 Worker 和 Runner 镜像，确认能力注册。
3. 发布 RuntimeProfile；新 Run 使用新版本，旧 Run 保留原版本。
4. 逐步切入测试团队与低比例流量，观察错误、费用和任务恢复。
5. 旧版本 Run 结束或完成明确迁移后，才下线对应 Worker/镜像/Skill。

### 18.4 运维动作

| 动作 | 条件与结果 |
| --- | --- |
| 暂停团队派发 | 允许受理但排队，或按策略返回限流；不丢已有任务 |
| 停用工具 | 立即拒绝新的执行；现有运行保留不可用证据并部分结束或转人工 |
| 重新派发 | 仅对已确认可恢复的非终态 Run，由协调器创建新 Job generation |
| 外部动作对账 | 使用 action_id 查询外部结果，保存事实回执；无事实不能标成功 |
| 回滚 Profile | 新 Run 使用旧发布版本，活动 Run 不被静默换版 |
| 回滚数据库 | 需要先停止写入并协调业务/检查点/动作记录，不能按普通应用镜像回滚处理 |


### 18.5 后端选择示例

| backend | SQLAlchemy URL 示例 | 驱动 |
| --- | --- | --- |
| sqlite，首版 | sqlite+aiosqlite:////var/lib/workbench/workbench.sqlite3 | aiosqlite |
| mysql，后续 | mysql+asyncmy://user:password@db-host:3306/dod?charset=utf8mb4 | asyncmy |
| postgresql，后续 | postgresql+psycopg://user:password@db-host:5432/dod | psycopg |

连接 URL 在配置中通过秘密引用加载，示例不是真实凭据。选择后端只改变应用连接和适配器；已有数据需要完成第 21.3 节迁移，不能仅切换 URL。

## 19 可观测性和评估

**范围与状态：**已实现结构化 Logging、API 到 Worker 的 OpenTelemetry 上下文传播及 Langfuse SDK 导出适配，默认不启用 Tracing 和 Langfuse。模型、Skill、工具传输和 Scheduler 扫描已有观测；动作、Runner 和细粒度 Graph 节点等仍待覆盖。真实 Langfuse 服务尚未联调验收。Prometheus、`/metrics`、指标 exporter 和指标告警延期。升级、配置和限制见 [可观测性手册](../runbooks/observability.md)。

### 19.1 Trace 与日志

目标契约是以 Trace 关联 ingress request、Run、Job attempt、Graph node、expert subtask、模型调用、ToolInvocation、Runner 和 Action；结构化日志携带 request_id、team_id、agent_id、thread_id、run_id、job_id、run_epoch 和 trace_id，不输出秘密或完整敏感原始数据。该契约不表示当前已经接通完整 Trace 平台或交互查看页。

当前模型日志与持久记录的实际边界如下：

| 项目 | 当前状态 |
| --- | --- |
| 调用尝试日志 | ModelInvoker 记录 `model.attempt`、`model.completed` 和 `model.failed`，包含调用标识、角色、尝试次数及可取得的状态、耗时和用量 |
| 终端可见性 | API、Worker、Scheduler 的 `dod` logger 默认输出 INFO 级 JSON 到 stderr；第三方及其他 logger 不承诺使用同一格式 |
| 用量与结果 | 调用层结算 usage 台账；工作流保存归一化 outcome。Run 固定的 Profile/模型/Skill 引用可用于追溯，但不等于完整请求快照 |
| 原始交互 | 当前不打印完整 system prompt、消息历史、供应商 HTTP 请求体或原始响应，也没有完整 LLM 交互查看 UI |
| Tracing 与 Langfuse | 开启后，受理上下文保存在 `run_trace_contexts` 并由 Worker 续接；使用 Langfuse SDK 导出，真实服务尚未联调；业务 trace_get 工具查询的外部 Trace 不等于平台自身调用链 |

因此，任务结果或回答风格不能单独证明某段 Skill 已被实际发送；开发验收应检查模型适配层收到的消息及固定引用。浏览器只能看到工作台 API 流量，看不到 Worker 向供应商发出的服务端请求。请求与响应边界见第 7.3.2 节。

模型正文采集仅能通过部署配置显式开启；脱敏不是完全隐私保护，须先审批数据范围、访问权限与保留周期。普通日志不记录模型正文、Authorization、真实密钥或内部思考。

### 19.2 指标（延期，不纳入本轮实现）

以下仅保留后续容量治理的指标候选，不作为本轮开发、依赖安装或部署要求；当前以结构化日志、Trace 和已有 usage 台账排障，不新增 Prometheus 服务。

| 指标 | 用途 |
| --- | --- |
| dod_run_admission_seconds | 受理延迟 |
| dod_job_queue_age_seconds | 队列积压与公平性 |
| dod_run_active / waiting / terminal_total | 执行、等待与结果分布 |
| dod_checkpoint_write_seconds / failures_total | 检查点健康 |
| dod_storage_busy_total / write_transaction_seconds | SQLite 写忙与短事务耗时，按后端标识统计 |
| dod_sqlite_wal_bytes / backup_last_success | SQLite WAL 大小与一致备份健康 |
| dod_recovery_total / duration_seconds | 故障恢复 |
| dod_model_requests / tokens / cost | 模型用量与费用 |
| dod_tool_calls_total{tool,status} | 工具成功、空结果和故障 |
| dod_action_unknown_total / outbox_oldest_age | 不确定动作与交付积压 |
| dod_budget_exhausted_total | 预算控制 |
| dod_feedback_score / knowledge_gap_total | 反馈与知识质量 |

若后续恢复指标建设，run_id 等高基数字段仅进入 Trace/日志，不作为 Prometheus 标签。按团队统计的访问权限与业务权限一致。

### 19.3 费用与效果

每个模型尝试记录请求用量、价格版本、实际或估算费用；最终结果区分已结算和 usage_pending。主任务、专家、评估费用按同一 Run 汇总，但保留子任务明细。

P0 效果评估包含证据引用覆盖、工具失败被正确表达、必需步骤完成情况、用户反馈和知识缺口。根因正确性使用带人工标签的业务案例评估，不能由模型自评分代替。Knowledge Gap 区分未命中、证据冲突、内容过期和检索服务不可用。

### 19.4 分层架构与实现职责

采用一套 OpenTelemetry Trace 上下文，通过 Langfuse 适配导出执行 Span 和模型 Generation；不为 Langfuse 再创建一套互不关联的 Trace ID。Langfuse 支持接收 OpenTelemetry 数据，但不是本方案的普通应用日志存储。[F19][F20]

| 层次 | 设计职责 | 不承担的职责 |
| --- | --- | --- |
| Logging | 标准 logging 统一输出单行 JSON 到进程 stderr，由部署环境采集；关联业务标识、trace_id/span_id、耗时和错误码 | 默认不记录问答正文，不在应用中再维护一套日志数据库 |
| Tracing | API、Job 执行尝试、Graph 节点、Skill 加载、模型、工具及动作形成有父子或 Link 关系的 Span | 不以 Span 替代 RunEvent、检查点、幂等记录或权限检查 |
| Langfuse | 展示执行链路、模型输入/输出（经授权开启时）、版本、tokens、耗时及错误；session_id 对应业务 Thread | 不替代业务用量结算、模型凭据管理或 Agent/Skill 发布系统 |

应用内只初始化一个明确拥有生命周期的 TracerProvider 和一条 Langfuse 导出路径；避免“自动 LangChain callback + 自定义 ModelInvoker Generation + 通用 OTLP exporter”重复导出同一模型调用。优先使用当前官方支持的集成；对本项目统一预算/重试包装层，由 ModelInvoker 明确建立每次实际供应商尝试的 Generation，框架回调仅在确认不会重复记录后启用。

本地 Logging 可独立启用，Tracing/Langfuse 由部署开关控制。Langfuse 可以对接经批准的自托管或 Cloud 实例，本方案不新增它的部署进程或 Compose 服务；若要求自托管，其数据库、对象存储和备份须另行规划。SDK `langfuse==4.15.1` 已锁定，目标服务兼容性仍须实测。

### 19.5 Logging 字段与采集边界

API、Worker、Scheduler 以及 CLI 共用 Logging 初始化，解决当前 Worker INFO 不可见和 extra 字段丢失的问题。按进程标识 `service.name`，所有进程携带同一 `service.version` 和 environment。建议默认 INFO；DEBUG 仅在受控环境短期开启，不能因此自动解除正文或密钥保护。

| 字段组 | 字段与约束 |
| --- | --- |
| 基础 | UTC timestamp、level、service、environment、event、message；event 为稳定名称，message 不拼接用户原文 |
| 关联 | request_id、team_id、agent_id、thread_id、run_id、job_id、run_epoch、invocation_id、attempt、trace_id、span_id；不存在的字段省略，不生成伪关联 |
| 结果 | status、duration_ms、error_code；队列另记 queue_wait_ms，模型另记 model_id、role、input_tokens/output_tokens（可未知） |
| 版本 | graph_version、Agent Profile 引用和固定 Skill 版本/哈希；不把 Skill 正文或 secret_ref 当普通日志属性 |

基础事件包括 request.completed、run.accepted、job.claimed/finished/failed、skill.loaded/unavailable、model.started/completed/failed、tool.completed/failed、action.dispatched/reconciled、telemetry.export_failed。SSE 记录连接建立/结束及状态，不逐 token、逐心跳打日志。无工作时的轮询不反复输出 INFO。

默认单条 JSON 日志上限 32 KiB；长消息截断并保留 truncated 标记。错误优先记录平台错误码和异常类型；堆栈仅保留安全的调用位置，不记录局部变量或可能携带 URL、请求体、凭据的原始异常文本。Authorization、Cookie、API Key、数据库 URL 和供应商认证头一律禁止输出。请求路径使用路由模板，不输出可能包含个人数据的完整 query string。日志轮转及保留由进程管理器/采集端负责，不能阻塞业务或无限占用本地磁盘。

### 19.6 Tracing 与跨进程关联

每次 Run 的执行链路采用如下逻辑层次；它是 Span 关系示意，不新增业务状态机或常驻服务：

~~~text
API request
  run.accept                    接受并冻结配置，提交后结束
    job.execute attempt=1       Worker 领取，记录排队耗时
      graph.node / expert
        skill.load
        model.generate attempt=1
        tool.execute
    job.execute attempt=2       恢复或重试的新执行片段
      graph.node / ...
    action.dispatch             仅实际发生的业务动作
~~~

API 不为了等待 Worker 而保持 HTTP Span 开启；已结束的受理 Span 仍可作为异步执行的父上下文。长时间人工等待不保留打开的 Span，每次领取、恢复、对账使用独立 Span，携带 run_epoch/attempt；取消、超时、租约失效和部分完成分别记录实际结果，不能统一标为成功。

跨进程上下文不能仅放在进程内 ContextVar 或 Redis 通知中。[F20] 迁移 `0003_run_trace_contexts` 新增 `run_trace_contexts`，保存 team_id、run_id、内部签发的 traceparent 和 created_at；以 run_id 为主键，并用 `(team_id, run_id)` 外键关联 runs。只保存受限长度的 W3C Trace Context，不保存 baggage、令牌、正文或端点。历史 Run 无关联记录时仍可执行。

在 Run 受理事务内登记已在内存生成的上下文，不在事务内初始化 SDK 或访问遥测网络；幂等重放复用原 Run 的上下文，不覆盖它。Worker 在领取后恢复上下文、执行结束后清理 ContextVar；同进程并发 Run 不得串链。禁用采集、历史 Run 无上下文或上下文无效时正常执行，创建独立执行 Trace 并保留 run_id，不为补 Trace 重跑模型。存储自身故障仍遵循业务数据库错误处理，不能借“遥测降级”吞掉事务失败。

初期公共入口不直接信任浏览器提交的 traceparent/tracestate/baggage，由服务端生成内部上下文；受信网关传播须显式配置来源校验。不向未经授权的供应商、MCP 或其他下游自动传播 Trace 头和业务标识。Scheduler 无父 Run 的扫描使用短生命周期 Span；首次覆盖任务受理到实际 Worker 调用，Dispatcher/Runner 仅在对应真实执行入口接入，不为占位能力创建虚构 Span。

### 19.7 Langfuse 的模型交互数据契约

| Langfuse 数据 | 本项目映射与实现要求 |
| --- | --- |
| Trace/Observation | 使用平台统一 trace_id，携带 team_id、agent_id、run_id、graph_version 和固定 Profile 引用；名称采用 run.execute、knowledge.answer 等稳定语义 |
| Session/User | session_id 映射 Thread；主体使用受控的团队范围伪名，不直接上传邮箱、令牌 subject 或姓名 |
| Generation | 每次真正发给供应商的尝试记录 invocation_id、role、attempt、模型名、温度/输出限制、开始/结束时间与归一化错误码；预算检查前失败不能伪造已调用 |
| 输入 | 在 ModelInvoker 最终发送边界采集 SDK 消息的规范化角色/内容及受控工具 Schema；须包含实际生效的系统提示、Skill 和历史，而不是读取最新草稿重新构造 |
| 输出 | SDK 解析后的 content、受支持的 tool_calls、finish_reason 与 usage；不是供应商原始 HTTP 响应，不采集认证头或隐藏推理内容 |
| 版本归属 | 单独记录固定 Agent Profile、模型和 Skill 的 ID/version/hash，便于核对实际绑定；无需将现有 Prompt/Skill 管理迁到 Langfuse |
| 用量与费用 | tokens 采用真实 usage，未知值保留未知，不填 0；平台 usage 台账是结算事实源，Langfuse 估算费用不得覆盖实际/待结算状态 |

上表是目标数据契约。当前 Generation 已记录模型名、调用标识、角色、尝试次数、固定 Skill 引用及可取得的 token；受控正文模式仅采集文本 system/human/ai 消息与文本回答。工具 Schema、tool_calls、finish_reason、完整 Profile 版本属性和伪名用户映射尚未接入，不能从 Langfuse 假定这些信息完整。

默认 content_mode=metadata_only，仅发送允许字段、版本、数量、耗时和状态，不发送正文、原始 Schema 或内容哈希来替代脱敏。管理员显式开启 redacted_content 后，才允许在团队批准的数据边界内发送脱敏后的模型消息和回答；正文脱敏无法保证消除所有敏感信息，生产开启前必须审核数据类别、存储区域、人员权限和保留周期。[F21]

内容采集默认每次 Generation 的输入、输出各不超过 64 KiB，按完整 Unicode 字符边界截断并标记 captured/truncated，原始字节数只作数值属性；采集截断不得影响实际模型请求。禁止采集 Header、密钥、环境变量、内部完整异常对象、reasoning_content 及模型内部思考。脱敏出错时退化为仅元数据，不上传未经处理的内容。

脱敏与属性白名单必须覆盖 Span 属性、事件、异常、框架回调及全部 exporter，在出进程前生效；不能只处理自定义 input/output 却让 SDK 自动采集原文。默认关闭 HTTP Body/Header、SQL 参数和异常全文的自动采集，受控适配器只填充明确允许的字段。

Langfuse 项目级可见权限不能自动等同工作台 Team/Agent 权限。默认不向普通工作台用户暴露项目密钥或直接跳转链接；不同团队需要隔离项目/访问权限，或先实现授权代理后再增加任务详情链接。工作台完整交互查看 UI 不纳入首次后端接入。metadata_only 模式下不能承诺在 Langfuse 看到完整提示词和回答。

### 19.8 配置、生命周期与实施顺序

以下为已支持的部署配置（密钥引用只接受 `env://变量名`）：

~~~yaml
observability:
  logging:
    level: INFO
    format: json
  tracing:
    enabled: false
    sample_ratio: 1.0
  langfuse:
    enabled: false
    base_url_ref: env://LANGFUSE_BASE_URL
    public_key_ref: env://LANGFUSE_PUBLIC_KEY
    secret_key_ref: env://LANGFUSE_SECRET_KEY
    content_mode: metadata_only
    max_input_bytes: 65536
    max_output_bytes: 65536
~~~

Logging 默认开启，不依赖 Langfuse；开启 Langfuse 要求同时开启 Tracing 并提供有效部署端点和密钥引用，不静默发送到 SDK 默认 Cloud 区域。禁止将真实 Key 持久保存在 Agent Profile、浏览器存储、RunContext 或版本库；管理端密码框仅在提交时短暂持有明文。Worker 配置管理端密钥存储时关闭模型正文采集。修改采集开关、端点或密钥需重启消费该配置的进程，不能通过普通 Agent 提示词改变。

采样在 Run 入口决定并跨进程保持一致，开发验收使用 100%；首次实现仅提供 head sampling，不承诺低采样下保留全部错误。SDK 使用有界批量异步导出，队列大小由 OpenTelemetry SDK 配置（默认 2048），网络请求超时 5 秒；关闭时后台尝试 flush，主协程最多等待 5 秒，不保证超时后遥测已送达。强杀可能丢失尚未导出的 Trace，遥测不具备业务级持久投递保证。队列满限频告警尚未验收。

非法配置在启动时给出脱敏错误；导出在后台进行，不用于 `/health/ready` 的业务存储判断。CLI 初始化与 API lifespan 管理 SDK 生命周期，不在数据库写事务中等待导出。断网、限流、队列满及关闭超时的全场景故障隔离仍需进一步验收。

| 顺序 | 拟实现落点 | 交付与验证 |
| --- | --- | --- |
| O1 日志底座 | 新增 observability 模块，在 settings.py、API/Worker/Scheduler 组合根初始化 | JSON formatter、上下文绑定与清理、脱敏、统一 INFO、日志大小限制 |
| O2 链路关联 | API 中间件、RunService、追加迁移、Worker 领取/恢复及 Graph 执行包装 | 请求与 Run 关联、上下文持久化、并发隔离、恢复/历史任务兼容 |
| O3 模型与资源 | ModelInvoker、SkillLoader、受控工具执行器和实际 Action 入口 | 每次真实尝试只记录一次、固定版本、错误/耗时/usage、预算拒绝不伪造调用 |
| O4 Langfuse | 独立 exporter/适配层与进程生命周期 | 端点授权、开关、脱敏模式、采样、批量导出、故障不影响业务 |
| O5 验收与操作 | 测试、启动手册及后续任务详情授权链接 | 自动化回归后，在获准 Langfuse 项目真实发送并查回 Trace；链接 UI 可单独延期 |

当前已实现 O1 和 O2 的主要链路，以及 O3 的 Skill 加载、模型尝试和工具传输 Span；Scheduler 扫描有独立短 Span。Graph 节点、动作和 Runner 的细粒度覆盖仍是目标。O4 已锁定 SDK 并接入模拟导出，生产服务与故障注入验收未完成；O5 的授权链接 UI 未开发。

### 19.9 验收与非目标

首次验收先跑通一个 v18 Run：API 受理 → Worker 领取 → 固定 Skill 加载 → 模型尝试 → 持久化结果。通过 run_id 可找到日志和同一 Trace，通过 session_id 可聚合同 Thread 的多次 Run；开启受控正文采集时，可核对实际消息顺序、Skill 固定版本和归一化响应，不仅观察答案风格。

目标自动化覆盖采集开关、格式/字段、无凭据与非法配置、并发 Run/团队隔离、ContextVar 清理、幂等受理、Worker 重启恢复、旧 Run 无上下文、模型重试不重复计数、401/超时/取消/租约失效、预算拒绝不发送、脱敏和 Unicode 截断。目标还包括模拟 exporter 的断网、429、队列满和关闭 flush；任意日志与导出载荷中均不得出现测试密钥、Authorization 或未经允许的正文。目前已验证部分配置、跨进程关联、幂等重放、Skill 与模型消息脱敏、模拟导出失败和退出等待超时，不能据此宣称全部故障场景已验收。

真实验收须在用户批准的 Langfuse 项目中提交合成/非敏感问题，查回 Trace 与 Generation，确认父子关系、版本、状态、tokens 和 session 归属，并记录成功与失败案例。当前仅验证 SDK 模拟导出与本地链路，尚未配置项目、端点或凭据，不能宣称真实服务已接通。

本轮不实现 Prometheus/指标告警、通用日志检索平台、自托管 Langfuse 基础设施、自动评估/数据集实验、Prompt 托管迁移或业务账单替代。后续工具、Runner 与企业连接验收逐步补齐实际链路，不为尚未发生的操作生成成功记录。

## 20 我的工作台与管理端

管理界面采用 Vue 3、TypeScript、Vite、Element Plus 和 Vue Router。构建阶段使用 Node.js 与锁定的 npm 依赖，产物位于 `apps/web/dist/`；Agent 镜像将其复制到 `/opt/workbench/web/dist/`，通过 `WORKBENCH_FRONTEND_DIST_ROOT` 指定并由 FastAPI 与 `/v1` 同源提供。Vue Router 使用 `/app/` 基路径，深层页面刷新回退至 SPA 入口；缺失静态资源返回 404，API 和 SSE 路由不回退。运行镜像无需 Node.js。界面只消费经过授权的业务 API，认证和 SSE 回放遵循第 11、12、17 节。

### 20.1 产品信息架构

统一产品名称为“我的工作台”，主导航包括工作台、任务记录、会话、事件中心，管理导航包括 Agent 管理、模型配置、Skills、工具连接、定时任务、数据统计。工作台承载多个独立可配置 Agent，DoD 是其中一个场景；不设置 Workspace 菜单或领域层，也不增加 `agent_type` 枚举或市场。源码变更与验证状态见第 21.4 节。

每个 Run/Thread 都必须关联一个具体 Agent，不能只显示通用“问答任务”而丢失归属。页面展示 Agent 名称并保留可追溯的标识；名称可变化，历史执行依据以 Run 固定版本为准。

### 20.2 当前页面与交付状态

| 页面与当前路由 | 已实现 | 边界与后续工作 |
| --- | --- | --- |
| 工作台 `/app/` | 选择 Agent、提交问题、进入任务；支持直接问答与知识问答入口 | 不同入口须匹配工作流，直接问答不要求知识源；提交成功不代表模型已完成 |
| 任务记录 `/app/runs` | 真实任务列表，按 Agent、状态和查询条件筛选，游标分页 | 不得混入示例任务；服务失败不能显示成“没有任务” |
| 任务详情 `/app/runs/:id` | 查询持久化状态、已保存结果及关联 Agent；活动任务轮询刷新 | 完整证据、工具时间线、费用、产物、取消和审批等交互仍需增量实现；不能把后端 SSE 能力当作完整流式 UI |
| 会话 `/app/threads` | 会话列表、创建/查看会话、关联 Run 和连续追问 | 连续追问要求 v18；同一会话不跨 Agent，不并发接受显式追问 |
| Agent 管理 `/app/agents` | 创建、读取、草稿编辑、工作流/模型选择、保存与显式发布，行为、上下文和预算配置；添加/移除 Skill 与选择版本 | 技能变更先修改本地草稿，保存并发布后才影响新 Run；绑定资源不等于当前图会执行工具 |
| 模型配置 `/app/models` | 模型草稿、版本发布和显式探针 | 端点、模型名、温度、输出/上下文限制和超时在这里管理；密钥只存引用，实际凭据由部署提供 |
| Skills `/app/skills` | 团队共享库查询、创建、草稿编辑/保存、发布与历史版本内容查看，未保存提示 | 当前编辑单个 Markdown 指令；不提供多文件包编辑、远端拉取或自动升级 Agent |
| 工具连接、定时任务、事件中心、数据统计 | 当前均使用 Placeholder 页面 | 真实管理 UI、权限/空态/错误态及业务验收待开发；已有后端 API 不能视为页面完成 |
| 演示详情 `/app/demo/runs/:id` | 显式隔离的演示页面 | 不属于真实任务记录或真实业务验收 |

此表依据当前路由与视图实现，不表示所有能力通过生产验收。占位页面或样例数据应明确标识，不得伪装成当前账号的真实记录。

### 20.3 Agent 配置契约

配置生命周期为“创建 Agent → 编辑草稿 → 保存 → 显式发布 → 新 Run 固定该发布版本”。保存失败保留可修正的输入，版本冲突提示重新读取；不得把“保存草稿”显示成“运行配置已生效”。

新建 Agent 从团队级（`scope=team`、`agent_id=NULL`）的已发布模型版本初始化 `default_model`，不读取或复制现有默认 Agent 的 RuntimeProfile，也不借用其他 Agent 的私有模型。系统提示词、描述、回复偏好、历史窗口、预算、角色模型及 Skill/工具/仓库绑定按新草稿的领域默认值初始化；管理员须为新 Agent 显式配置并发布，部署凭据白名单也须单独授权。缺少可用团队级模型时返回 `MODEL_PROFILE_UNAVAILABLE`，不通过继承另一个 Agent 配置补齐。默认草稿仍为仅记录输入的 `dod.v3`；直接问答须显式选择 `dod.v17` 或 `dod.v18`。

创建 Agent 不依赖任何现有活动 Agent，使用 `team_idempotency_records` 保存团队/主体/路由/键范围内的原始响应。`auth.agent_scope` 受限的身份不能创建范围外的新 Agent；不得通过选用某个已获授权的默认 Agent 作为模板或幂等锚点绕过创建限制。

| 配置项 | 当前字段与约束 | 生效范围 |
| --- | --- | --- |
| 名称与描述 | 名称标识管理对象；description 默认空，最多 1000 字符 | 描述用于管理说明，不自动作为模型指令 |
| 系统提示词 | system_prompt 默认空，最多 12000 字符 | v17/v18 注入当前 Run 的系统指令；不改变部署权限与工具授权 |
| 工作流与模式 | graph_version；allowed_modes 默认包含 auto/qa/rca/code/sop/inspection | 实际受理还须符合图的模式约束；直接问答仅 qa |
| 默认与角色模型 | default_model 必填 VersionRef；role_models 可选 | 使用精确模型版本，summary 等角色未绑定时回退默认模型 |
| 回复语言 | response_language=auto/zh/en，默认 auto | v17/v18 提示偏好，不是严格语言检测 |
| 回复格式 | response_format=default/markdown/json，默认 default | 提示偏好，不保证有效 JSON；严格结构化输出须独立校验 |
| 上下文管理：对话历史轮数 | conversation_history_turns 为整数 0–8，默认 8 | 仅 v18；每轮含一问一答，0 不传历史但保留记录，实际还受模型上下文和预算限制 |
| 运行预算 | budget_limits 可选；wall_seconds/model_calls/tool_calls/tokens 为正整数或 null | 接受时与系统及请求限制合并；未设上限不等于无限使用 |
| Skill 绑定 | skills 为精确 VersionRef 集合；新保存/发布最多 16 个不同 Skill | v18 加载 SKILL.md 指令；v17 不加载。升级版本须显式修改绑定并发布 Agent |
| 其他高级资源引用 | tools/repositories 及高级 JSON | 保留合法字段，按发布与工作流契约解析；v17/v18 不执行工具或仓库操作 |

“上下文管理”目前仅管理近期历史问答的输入窗口，不包含自动压缩摘要、知识库、长期记忆或跨会话记忆。前端不应把该输入称为“保留历史总轮数”，避免误解为删除策略。配置清空必须显式表达：文本使用空字符串，可空预算使用 null；高级编辑不能因表单未展示字段而丢弃已有绑定。

#### 20.3.1 添加 Skill 的交互设计

Agent 配置区只展示已绑定技能，不再平铺所有技能并让用户逐行选择“未绑定”。标题显示绑定数量，提供“添加 Skill”入口；每个已绑定项显示名称、描述、版本选择和移除图标。技能创建、编辑与发布在独立 Skills 管理页完成。

| 操作或状态 | 交互与数据语义 |
| --- | --- |
| 打开添加弹窗 | 重新读取共享库，客户端遍历列表分页；聚焦搜索框，按名称或描述过滤当前已加载集合 |
| 查询和选择 | 列表采用多选框；默认绑定最新已发布版本。已绑定项与尚未发布项不可选，并显示对应状态 |
| 数量限制 | 现有绑定加待添加项不能超过 16；显示已选数量，避免重复添加或同一 Skill 的多个版本 |
| 添加所选 | 将待选技能追加到本地 Agent 草稿，关闭弹窗；不直接调用 Agent 保存或发布接口 |
| 取消 | 取消、关闭、Escape 或点击遮罩均丢弃本次待选，不改变已有绑定 |
| 更换版本与移除 | 在原位置替换固定版本、保留绑定顺序；移除只修改草稿，仍须保存并发布才生效 |
| 无数据与异常 | 区分无技能、查询无匹配、未发布和读取失败；列表加载失败不得清空原绑定 |
| 无法解析的历史引用 | 保留引用并显示“无法解析的技能”，不能因目录未返回该项而静默删除；新保存/发布仍执行合法性校验 |
| 键盘与切换 | 弹窗约束 Tab 焦点，关闭后回到添加按钮；切换 Agent 或令牌时清理待选和过期异步响应，避免跨身份串写 |

完整生效路径为“Skills 创建/编辑 → 发布 Skill 版本 → Agent 添加 Skill → 保存 Agent 草稿 → 发布 Agent → 新 Run 固定版本”。后续 Skill 新版本不自动替换旧绑定。16 项限制在新保存/发布时校验，不因此让旧配置详情无法读取。

### 20.4 身份、发布与异常体验

当前本地 UI 使用 Bearer Token，将其仅保存在当前标签页的 sessionStorage；这不是完整生产登录系统。新标签页或清除令牌后需重新提供身份，所有列表、详情、提交、发布和模型探针仍受后端授权约束。不得显示或记录真实模型密钥。

新建 Agent 后还需检查已发布配置、图版本、模型版本及部署凭据白名单。缺凭据或授权时应显示模型不可用的实际原因；不能让用户靠重新创建 Agent 解决部署授权缺失。发布模型后，Agent 须重新绑定并发布，新任务才会使用新模型。

空数据、无权限、服务不可用、排队中、执行中、部分完成和失败必须区分。排队仅表示任务已持久化等待 Worker；partial 不等于成功。UI 应保留 API 错误码及可理解提示，包括 `MODEL_PROFILE_UNAVAILABLE`、`THREAD_BUSY` 和工作流不兼容，而不是统一显示“数据不可用”。分析完成、通知待投递和外部动作待对账也应分开表示；进度不得以固定秒数生成虚假百分比。

### 20.5 工作台增量验收

当前交付顺序为任务记录 → Agent 管理 → 模型配置 → 会话，以真实数据闭环为门槛。至少覆盖：选择 Agent 发起真实 LLM 任务并查回结果；筛选任务且刷新后归属不丢失；发布系统提示词后仅新任务生效；同会话第二轮能引用第一轮信息；历史轮数为 0 时不传历史；不同 Agent/会话及越权历史不能混入。测试断言应检查模型请求消息与固定配置，不能只凭回答“看起来记住了”判断上下文正确。

共享 Skills 增量验收再覆盖创建/保存/发布、团队权限隔离、幂等与并发冲突、历史版本回看、添加弹窗的查询/多选/取消/数量上限、版本替换顺序及异常时保留引用。运行时检查 v18 的实际模型输入包含固定 Skill 指令，旧 Run 不随新发布变化，不可用 Skill 阻止模型调用；不以模型碰巧遵循某种回答格式作为唯一证据。该范围的回归不替代真实模型鉴权、完整 DoD 工具链或生产部署验收。

## 21 迁移与发布

### 21.1 迁移单位

本节历史系统迁移与第 21.4 节的 Agent 领域重构分开：前者导入业务资产，后者移除旧 Workspace 产品与领域概念。

先迁移团队配置、Skill/资源、MCP 绑定、代码仓库、通知渠道和必要业务连接器，再切换任务入口。每个转换条目保存来源标识、版本、内容哈希和转换结果，支持差异核对。

| 资产 | 迁移方式 | 校验 |
| --- | --- | --- |
| 团队/Workspace（历史来源） | 显式映射到 Team/Agent | 归属、成员、环境和模型范围 |
| SKILL.md 与资源 | 导入并生成新 SkillVersion | 文件路径、工具 alias、脚本依赖和输出 |
| CLAUDE.md/团队规则 | 转为 Agent 规则与上下文 | 区分指导内容和必须由代码实现的策略 |
| .claude/agents | 转专家目录或场景配置 | 工具范围、Skill 引用与结构化接口 |
| MCP | 复用标准协议及连接器 | Schema、身份作用域、错误、取消和实际调用 |
| Python/Node/Shell 脚本 | 作为 Runner 资源运行 | 镜像依赖、资源限额、幂等与产物 |
| 历史事件和知识 | 引用或按 Schema 导入 | 来源、证据状态、时间和权限 |
| 历史费用 | 只作为带来源的历史报表 | 不与新 Run 费用去重键混用 |

旧 SDK session 不能直接转成 LangGraph 检查点。历史对话可以导入为来源明确的消息/摘要；新运行建立新 Run，不声称从旧 SDK 的任意执行位置继续。

### 21.2 切换策略

按团队和场景发布 engine=langgraph 的 Profile，新任务进入新系统；在途旧任务继续由旧引擎处理，直到终结。告警去重和通知关联通过迁移路由记录防止两套系统同时执行同一来源事件。

需要对比效果时，影子 Run 禁止对外写操作和通知，保存独立对比结果与费用。回滚只影响新受理任务；新系统内已有 Run 仍由其原版本 Worker 完成。


### 21.3 SQLite 迁移至 MySQL 或 PostgreSQL

P0 约定可迁移的逻辑格式，后续适配阶段交付 export/import/validate 命令。迁移采用维护窗口，不承诺活动写入期间的无停机切换。

1. 验证目标 Backend 的迁移与共享契约测试，创建相同 schema_version 和 codec_version 的空目标库。
2. 入口进入维护状态；停止派发并等待当前数据库短事务完成，停止 Worker 和 Dispatcher。允许已持久化 waiting Run 留在库中，禁止两个后端同时继续同一 Run。
3. 对 SQLite 做一致备份，逻辑导出团队/配置、业务记录、ID/外键、事件 seq、动作/幂等记录，以及完整检查点、父链和 pending writes；包内记录 Schema、Codec、Graph 版本、行数和摘要。
4. 目标后端按外键顺序导入，保留 UUID、Scope、命名空间、seq、摘要和 checkpoint typed bytes。对 active_run_id、accepted_resume_request_id 等循环回指先导入 NULL，再在目标行存在后补齐并校验引用；不能依赖某一数据库专属的延迟外键。不能通过“最终消息列表”代替运行检查点。
5. 使迁移前执行租约失效、清空旧 owner；非终态 Run 的新执行以新 epoch 和 Job generation 接管。历史动作回执与 action_id 保留，unknown 仍进入对账。
6. 校验逐表行数/摘要、引用完整性、事件序号、检查点父链和 Codec 解码；抽验 waiting 恢复、SSE 续传、未投递 Outbox 与重复请求。
7. 以新后端启动 doctor、API 和 Worker，先只读/小流量验证；完成动作对账后开放 Dispatcher 和正常入口。SQLite 旧库保持只读作为回退基线。

目标库已经产生新写入后，回滚不能直接指回旧 SQLite 文件；需要停止新写入、反向迁移或按记录对账，防止丢失新 Run、覆盖 epoch 或重复外部动作。迁移 CLI 属于后续实现契约，本次仅更新设计文档。

### 21.4 移除 Workspace 的 Agent 领域重构

**状态：文档、前后端 Agent 领域重构及本地回归已完成，尚未部署到现有实例。** 本次使用临时数据库和模拟模型验证，不代表真实模型或企业业务连接验收。未修改实际运行数据库。允许重新配置业务资产，但现有数据库必须保留，代码、迁移和启动过程均不得自动删除数据库或业务数据。

采用 Agent 直接替代 Workspace 的破坏性契约，在显式选择的全新 SQLite 路径初始化并重建配置，不保留旧 API 双写或永久别名。“我的工作台”可配置多个独立 Agent，DoD Agent 是其中一个场景；不增加 Workspace 容器、`agent_type` 枚举或 Agent 市场。LangChain 专家继续作为图内部执行角色。

| 层次 | 重构目标 | 必须保留的不变量 |
| --- | --- | --- |
| 领域与存储 | 以 `agents` 表和 `agent_id` 替代旧 Workspace 归属；RuntimeProfile 仍管理草稿/不可变发布版本 | Team → Agent → Thread/Run 的归属，唯一性、外键、状态与 revision 校验 |
| Run 与配置 | RunContext 使用 agent_id、agent_profile；Agent 版本承接模型、工作流、行为、上下文、预算及资源引用 | 受理冻结、旧执行不读取新草稿、secret_ref 不含真实密钥；新存储/快照版本不得误读旧数据 |
| 新建 Agent | 使用团队级已发布模型和独立的新草稿默认值，不复制现有默认 Agent 的配置 | 不继承其他 Agent 的系统提示词、行为、上下文、预算、角色模型、资源或凭据授权；缺少团队级模型时明确失败 |
| 身份与凭据授权 | `workspace_scope` 改为 `agent_scope`；模型凭据、连接器白名单统一使用 `agent_id`/`agent_ids` | 团队成员、角色、Agent 范围和工具权限交集保持；移除概念不等于取消权限隔离 |
| API 与前端 | `/v1/agents`、`/app/agents`、`Agents.vue`、`agents-api.ts`，请求/响应/过滤条件统一 agent_id | 创建、幂等、保存、发布、任务列表/详情及会话同步调整；旧接口明确不可用，不能静默忽略旧字段 |
| 资源与调度 | 模型、Skill、工具、仓库、Schedule、Incident、上传与幂等记录中的原 Workspace 归属同步改为 Agent | 资源授权、去重范围、任务领取、结果及证据归属一致；团队共享资源保留明确的可空 Agent 作用域 |
| 文件执行 | `workbench_agent.execution` 替代旧 workspaces 包；`tools.execution_executor` 提供 `ExecutionExecutor`/`ExecutionBinding`；容器路径 `/workspace` 保持技术含义 | 保留目录隔离、快照、路径校验、Runner 清理和产物上传能力，不能因删除产品概念而删除执行安全边界 |
| 运维与文档 | 更新 bootstrap、部署配置示例、运行手册、OpenAPI、测试和历史图示状态说明 | 密钥不落库，启动命令不自动删库；历史验收记录保持历史语义并注明新基线 |

发布步骤：保留旧数据库及 WAL/SHM、备份和对象存储；显式设置新的绝对 `WORKBENCH_DATABASE_URL`，在独立新 SQLite 文件上执行 migrate、bootstrap 和 doctor。重建成员、模型/凭据授权及 Agent 并测试；确认目标实例后停止 API、Worker、Scheduler，统一切换到新数据库路径和新版配置，再重启验收。迁移命令、doctor 和 readiness 必须拒绝旧 Workspace Schema，提示选择新路径及显式初始化；不得自动删库、清表、修改旧库或自动切换路径。新旧配置、存储协议或 Codec 不匹配也须拒绝 ready。

回归覆盖新库就绪与重复初始化、旧 Workspace 库迁移/就绪安全拒绝且数据未被修改、Agent 创建/保存/发布、持久任务归属、v17/v18 问答、上下文限制、跨 Agent/团队拒绝访问、模型及工具授权、恢复/幂等/调度。新库初始化在排他锁下复核，缺少或弱化 CHECK、非 WAL 的已初始化库均拒绝；命令行输出安全的显式初始化指引，其他异常不暴露底层内容。历史迁移测试直接构造历史夹具，不用新版 Bootstrap 或服务读取旧 Workspace 表。

2026-11-17 本地验证结果：

| 范围 | 执行结果 | 边界 |
| --- | --- | --- |
| 后端全量 `python -m pytest -q` | 1148 通过，17 跳过 | 临时 SQLite；跳过项不作为通过项 |
| 后续 CLI 提示改动回归 | 53 项存储/就绪/历史边界/Resume 测试通过；5 项打包测试通过 | 验证旧库提示、异常脱敏、文件不变和 wheel 安装 |
| 前端 `npm test -- --run` | 16 个文件、65 项测试通过 | 包含跨 Agent 异步隔离、草稿放弃确认及提交期间禁用切换 |
| 前端类型检查与构建 | `npm run typecheck`、`npm run build` 通过 | 已生成构建产物，不代表现有 API/Worker 已升级 |
| 数据字典 | 59 张表、575 个字段与 metadata 一致 | 未执行用户库 DDL |
| 浏览器闭环 | 创建非 DoD Agent、独立提示词、保存发布、提交问题、完成结果和任务列表归属通过 | 隔离临时数据库、模拟模型；测试服务已停止 |
| 静态检查 | 文档本地链接/锚点检查和 `git diff --check` 通过 | 历史图集未重绘 |

本次没有重新验收真实 LLM、企业知识源、SeaTalk/Jira、容器或生产部署。真实调用应在用户授权并配置好模型凭据的环境单独执行。

本文第 5、11、13、18、20 节与源码已统一为 Agent 契约。历史验收、历史计划和图表快照保留原来的 Workspace 语义，不能作为新 Schema 或新 API 的验收证明。新库启动说明见 [本地启动手册](../runbooks/source-layout.md) 和 [SQLite 运维手册](../runbooks/sqlite-recovery.md)。

### 21.5 共享 Skills 的增量升级与交付边界

这次升级不同于第 21.4 节移除 Workspace 的破坏性领域切换：已有 Agent 数据库沿独立 Agent 迁移链追加 `0002_shared_skills`，后续可继续升级到 `0003_run_trace_contexts`，保留 Agent、任务、历史版本和私有 Skill，不创建替代数据库。旧 Workspace Schema 仍拒绝沿此链升级。

1. 停止 API、Worker、Scheduler，按 SQLite 运维流程制作一致备份，同时备份 Skill 对象目录；不要在运行中仅复制数据库主文件。
2. 部署新代码并构建前端，在所有进程环境中配置相同的绝对数据库路径和 `WORKBENCH_SKILL_OBJECT_ROOT`。现有对象目录须继续保留，不得用空目录替代后声称历史版本可用。
3. 从 `apps/agent` 显式执行 `uv run workbench db migrate --config "$WORKBENCH_ROOT/config/local/agent.yaml"`，再执行 `uv run workbench doctor --config "$WORKBENCH_ROOT/config/local/agent.yaml"`。已有 Agent 库不因本功能要求重跑 bootstrap，更不能删库初始化；普通服务启动不自动迁移。
4. 重启三个常驻进程，确认原任务和 Agent 可读，再验收 Skills 创建/发布、Agent 添加与发布及 v18 加载。`doctor` 就绪不代替对象读写和真实模型连通性验证。

仅修改 Skill 内容并发布、或修改 Agent 绑定并发布，不需要重启服务；首次引入后端代码/迁移或改变部署对象路径时需要重启相关进程。仅更新前端交互时重新构建并替换静态资源，浏览器刷新加载新文件；没有新增前端常驻服务。回退数据库须使用已审核的一致备份恢复流程，不能通过直接降级迁移或删除 skill_drafts 绕过版本兼容。

当前代码包含共享库后端集成测试、v18 消息注入与失败边界测试、Skills 页面/API 测试及 Agent 弹窗交互测试；前序本地回归和浏览器交互已验证部分链路，不代表完整生产验收或本次重新调用过真实模型。本文更新只同步设计与实现边界，不修改运行数据库、业务技能或 Agent 配置。原始设计和实施范围见 [共享技能库设计](../superpowers/specs/2026-11-17-shared-skill-library-design.md)，操作见 [Skill 手册](../runbooks/skill-import.md)。

## 22 容量和服务目标

### 22.1 首轮容量目标

| 项目 | 验收目标与条件 |
| --- | --- |
| 受理延迟 | SQLite 本地持久磁盘健康、低竞争负载时 p95 ≤ 500 毫秒；不含模型执行 |
| 在线执行 | SQLite 首版一个 Worker、2 个并发 Run；后续 MySQL/PostgreSQL 的 20 并发目标另行容量验证 |
| 团队公平性 | SQLite 首版单团队最多 2 个活跃 Run；多团队轮转，不因候选扫描长期占有写事务 |
| 任务恢复 | 数据库与工具可用、执行租约可原子接管时，Worker 故障后 90 秒内安排恢复 |
| SSE 回放 | 保留期内游标可恢复；重复事件按 seq 去重 |
| 事件可靠性 | 数据库提交确认后不依赖进程内存，进程重启不丢受理任务或已提交事件 |
| 操作一致性 | 同一逻辑动作具有稳定幂等标识；不确定结果可见并可对账 |
| 资源控制 | 每次模型/工具执行受父 Run 预算约束，不能靠恢复刷新额度 |

数据库故障切换时间、外部模型速率、下游超时和 Runner 容量会影响端到端耗时，需分别度量。目标数值必须在后续实现的容量与故障测试中验证。

### 22.2 容量约束

SQLite WAL 允许读写并行，但同一时刻仍只有一个写事务；增加进程或连接数不能消除该约束。[F7][F8] 首版使用单机持久磁盘、一个 Worker 和 2 个并发 Run，按事件合并、短事务、及时关闭读事务和批量清理控制写压力。

持续写忙、事件落盘延迟、WAL 增长和队列等待超过验收目标时，优先限流并降低并发；需要多主机或更大写入吞吐时再迁移服务型数据库。不得把 SQLite 文件挂载到 NFS 作为横向扩容方案。

后续 MySQL/PostgreSQL 按连接池总量和事务竞争验证 20 个活动 Run 等更高目标；PortableCheckpointSaver 不要求每个 Run 持有长期专用连接。连接只在一次 UoW 期间使用，避免模型等待占满连接池。

Raw Evidence 与大文件存对象存储，GraphState 只保留摘要和引用。单个持久化图状态默认不超过 2 MiB；输入文本默认不超过 64 KiB，模型上下文按 ModelProfile 上限压缩。无法保留必需证据时返回明确上下文超限结果。

### 22.3 数据恢复目标

进程故障恢复依赖当前 StorageBackend 已提交记录，存储故障恢复依赖部署的复制/备份。首版生产部署要求执行一次数据库恢复、对象存储引用校验和 Action 对账演练后再开放外部写动作。

## 23 验收用例与完成标准

### 23.1 需求覆盖

| 用例 | 覆盖需求 | 场景与通过条件 |
| --- | --- | --- |
| AT-01 | FR-01、FR-10 | API 同幂等键重复提交只生成一个 Run；提交后断线可查回原 Run |
| AT-02 | FR-01、FR-03 | SeaTalk 多轮问题回复到正确线程，补充输入指向正确 interrupt |
| AT-03 | FR-02、FR-08 | 跨团队读取、SSE、恢复、取消和工具调用均被拒绝 |
| AT-04 | FR-03、FR-06 | 真实知识检索产生可访问的引用，检索失败不伪装未命中 |
| AT-05 | FR-04、FR-05 | 真实告警按同一时间锚点查询监控/日志/Trace，并按需分析代码 |
| AT-06 | FR-04 | 区域或时间冲突产生明确追问，用户补充后继续同一 Run |
| AT-07 | FR-05、FR-08 | 专家使用权限交集，共享预算，并发上限有效 |
| AT-08 | FR-06、FR-07 | Skill 按版本加载，引用脚本可在 Runner 执行并上传可下载产物 |
| AT-09 | FR-07 | 同 Run 固定 commit；目录越界、超时和资源超限被控制并记录 |
| AT-10 | FR-09 | SOP 必需步骤失败后结果为 partial/failed，变量传递和分支可追踪 |
| AT-11 | FR-09 | 定时触发去重，错过合并和 overlap_policy 按契约执行 |
| AT-12 | FR-10、FR-13 | 运行中终止 Worker，租约到期并原子接管后从统一检查点恢复 |
| AT-13 | FR-10 | 中断保存后、waiting 投影前崩溃，协调器补齐等待点 |
| AT-14 | FR-10 | 用户回复应用后崩溃，恢复不重复投递同一 ResumeRequest |
| AT-15 | FR-10 | SSE 从任意已提交游标续传，进程重启后最终结果仍可查询 |
| AT-16 | FR-10 | 取消停止新操作；晚返回结果不覆盖 cancelled；finalizing 取消明确返回冲突 |
| AT-17 | FR-11 | Duty Event、Jira 和通知回执可关联到同一 Run；外部动作不因恢复盲目重复 |
| AT-18 | FR-11 | 模拟写入成功但丢失响应，Action 进入 unknown 并可对账 |
| AT-19 | FR-12 | 主任务、专家、重试和评估费用可汇总；未知计费状态不显示为零 |
| AT-20 | FR-12 | 反馈、知识缺口和基础统计可查；评估故障不改变主回答 |
| AT-21 | FR-10、FR-11 | Redis 断开后仍可受理、领取和回放，通知通过数据库继续发现 |
| AT-22 | FR-02、FR-10 | 发布新 Graph/Skill 后旧 Run 使用原版本恢复，缺版本不能静默替换 |
| AT-23 | FR-08、FR-10、FR-13 | 租约接管后旧 Worker 的 checkpoint/pending writes/事件写入均被同事务 Guard 拒绝 |
| AT-24 | FR-01、FR-11 | 旧/新系统切换时同一来源事件只有一个主动执行和通知路径 |
| AT-25 | FR-13 | SQLite 真文件重启后 Run、检查点、pending writes、SSE 和动作回执可恢复 |
| AT-26 | FR-13 | SQLite 多进程短事务竞争及 busy 超时，任务领取、seq、额度和 resume 指针均无重复或超发 |
| AT-27 | FR-13 | 检查点 Guard 与接管并发：旧写在接管前完成或被拒绝，不能在新 epoch 后提交旧快照 |
| AT-28 | FR-13 | SQLite Online Backup 恢复后完整性、外键、检查点父链和 Action 对账通过 |
| AT-29 | FR-13 | SSE 慢客户端不持有数据库读事务；WAL 回收不被连接等待长期阻塞 |
| AT-30 | FR-13 | 后续 MySQL/PostgreSQL 适配器运行同一 Repository/Job/Guard/Saver/预算契约集 |
| AT-31 | FR-13 | 后续跨后端迁移保留 ID、seq、Codec 字节、父链、pending writes、幂等和未知动作状态 |
| AT-32 | FR-13 | 后端与 URL 不匹配、SQLite 非持久路径或错误 PRAGMA/链接库版本拒绝 ready |
| AT-33 | FR-02 | DeepSeek 种子首次导入成功；重启/重复 bootstrap 不覆盖已发布配置；缺少 API Key 不伪造模型可用 |
| AT-34 | FR-02、FR-05 | Agent 模型版本及角色绑定按契约解析；排队、恢复和专家调用使用 Run 固定版本 |
| AT-35 | FR-03、FR-05 | DeepSeek 实测文本、工具循环、流式、JSON 校验与 usage；空白/截断 JSON 不视为有效结果 |
| AT-36 | FR-02、FR-12 | 密钥不进入数据库/事件/日志；SDK 与 ModelInvoker 不叠加重试，不可用时不静默换模型 |
| AT-37 | FR-01、FR-02、FR-12 | 工作台提交真实任务后可在列表及详情查回，并显示同一具体 Agent；刷新、筛选、分页不混入样例数据 |
| AT-38 | FR-02、FR-10 | Agent 保存草稿不改变已发布行为；发布提示词/回复偏好后新 Run 使用新快照，已排队任务仍使用旧配置 |
| AT-39 | FR-02、FR-03 | v18 第二轮模型请求包含授权的第一轮问答；0 轮不传历史且不删除记录，窗口和预算限制有效 |
| AT-40 | FR-02、FR-03 | 不同团队、Agent、Thread、无效摘要及未完成结果不混入历史；会话忙与不兼容工作流返回明确冲突 |
| AT-41 | FR-02 | 新 Agent 缺模型凭据授权时不伪造成功；授权后可调用指定模型，配置 API 不返回真实密钥 |
| AT-42 | FR-02、FR-13 | Agent 领域重构后新库初始化和重启就绪；旧 Workspace 库迁移/就绪安全拒绝且保持数据，API/Schema/前端归属统一，权限隔离与幂等回归通过 |
| AT-43 | FR-02、FR-06、FR-12 | 共享 Skill 创建、草稿保存与发布分离；团队隔离、角色限制、名称冲突、幂等重放、revision 冲突和不可变历史版本可验证 |
| AT-44 | FR-02、FR-06 | Agent 添加弹窗支持搜索、多选与最新已发布版默认值；已绑定/未发布禁选，最多 16 项；取消不写入，添加仅改草稿，异步切换不串写且异常不丢原绑定 |
| AT-45 | FR-03、FR-06、FR-12 | v18 模型输入含固定 Skill 文本及版本，v17 不加载；发布 v2 不影响旧 Run，损坏/撤权/缺失阻止模型调用，技能文本纳入上下文与预算且不授予工具权限 |
| AT-46 | FR-06、FR-13 | 已有 Agent 库增量迁移后 Agent/任务/私有技能不丢失；API/Worker 共享对象目录，迁移与备份恢复不自动删除或替换现有数据 |
| AT-47 | FR-10、FR-12 | API/Worker/Scheduler 日志统一 JSON 并关联业务标识及 Trace；INFO 可见、并发上下文不串写，任何日志不含密钥、认证头或默认禁采的正文 |
| AT-48 | FR-03、FR-10、FR-13 | 同一 Run 的受理、队列执行、Graph、模型/工具共享正确 Trace 关系；重启/重试/恢复保持关联，历史无上下文 Run 可执行，幂等重放不覆盖原上下文 |
| AT-49 | FR-05、FR-10、FR-12 | 在授权 Langfuse 项目查回实际模型尝试、版本、tokens、状态和 Thread Session；开关/脱敏/截断有效，模型调用无重复计数，元数据模式不上传正文 |
| AT-50 | FR-10、FR-12 | 遥测断网、限流、队列满与 flush 超时不改变业务结果或存储就绪；无事务内遥测网络调用，不新增 Prometheus 或 /metrics 依赖 |

### 23.2 测试层次

- 单元：状态转换、参数校验、版本解析、预算、规则与幂等键。
- 契约：模型网关、MCP、Runner、知识、监控、日志、Trace、Jira 和 SeaTalk。
- 集成：P0 使用真实 SQLite 文件、统一检查点、Job 领取、SSE 回放、人工恢复、事务与 Outbox；后续后端使用同一契约集。
- 故障：进程终止、数据库断连、租约失效、旧 Worker 晚写、丢响应与重复消息。
- 业务回归：至少 30 个带人工标签案例，覆盖正常答疑、告警异常、无数据、证据冲突、需要人工和预算不足；记录证据引用正确率和误报正常案例数。

P0 对“工具不可用却输出正常”和“越权访问”两类案例要求零通过漏检。初始业务集至少包含 30 个案例，其中不少于 10 个具有人工确认根因的告警案例；验收目标为证据引用正确率 ≥ 95%、人工认可的可用答复比例 ≥ 80%、确认根因案例的主要根因匹配率 ≥ 80%。同义表述由人工审核认定，不能以模型自评分代替。这些是有限验收集上的发布门槛，不代表生产总体准确率。

### 23.3 核心完成标准

以下为完整 DoD P0 完成标准，不是直接 LLM 工作台当前已全部完成的声明。工作台增量按第 20.5 节及 AT-37 至 AT-41 单独验收；Agent 领域重构另按第 21.4 节与 AT-42 验收，共享 Skills 按第 21.5 节与 AT-43 至 AT-46 验收。

1. 所有 FR-01 至 FR-12 及 FR-13 的 SQLite 首版范围均有真实可演示路径和对应验收记录。MySQL/PostgreSQL 适配与迁移在后续阶段完成 AT-30/31，不能以首版 SQLite 验收冒充三后端已可用。
2. 完整跑通告警接入、Skill、真实证据采集、代码分析、人工补充、事件记录和跟进交付。
3. 重启、重复请求、SSE 重连、取消、超时、未知外部动作和权限隔离用例通过。
4. API/OpenAPI、迁移、运行配置、启动命令和运维检查具备一致版本。
5. 管理界面可看到真实状态、工具证据、费用和外部回执，最终报告中保留未完成项。
6. 容量目标、故障恢复和备份恢复已执行验收；不以“SDK 调用成功”替代这些结果。

## 24 交付顺序与决策记录

### 24.1 分阶段交付

| 阶段 | 产出 | 独立验收 |
| --- | --- | --- |
| M1 运行底座 | 身份、Agent、Thread/Run、SQLite StorageBackend、统一 Job/Guard/Saver、SSE、恢复/取消 | AT-01、03、12–16、21、23、25–29、32 |
| M2 能力执行 | DeepSeek ModelFactory/Invoker、模型版本绑定、Skill、MCP、知识、仓库、Runner、专家与预算 | AT-04、07–09、19、33–36 |
| M3 DoD 链路 | RCA、SOP、巡检、人工参与、Event、DoD、Jira、通知 | AT-02、05、06、10、11、17、18 |
| M4 运营与发布 | 我的工作台、Agent 行为与上下文配置、任务记录、模型与会话；共享 Skills；Logging/Tracing/Langfuse；再扩展真实业务管理、反馈/知识缺口、容量与备份恢复 | AT-20、22、24、37–41 及完整端到端回归；Agent 领域收敛另验 AT-42，共享 Skills 验 AT-43–46，可观测性设计实施后另验 AT-47–50 |
| M5 后续存储适配 | MySQL/PostgreSQL Backend 与逻辑迁移工具 | AT-30/31 及共享契约、恢复与容量回归 |

各阶段采用可运行的纵向链路交付；M1 可用固定响应图验证任务语义，M2 起接真实模型和工具。固定响应图只验证底座，不能作为 DoD 业务核心完成的依据。

当前产品增量按“任务记录 → Agent 管理 → 模型配置 → 会话”形成可用闭环；前后端已统一为 Agent 领域契约。占位管理页面及真实 DoD 连接仍需独立开发与验收。M4 页面进展不表示 M3 真实业务已验收，后端 API 存在也不表示相应管理页面完成。

### 24.2 关键决策

| ADR | 决策 | 代价和替代方案 |
| --- | --- | --- |
| ADR-01 | 同一 Git 项目中的 Agent、Web、MCP Server 组件 | 各组件可分别构建；Agent 以多类进程运行，MCP Server 可独立部署 |
| ADR-02 | LangGraph 主工作流 + LangChain 专家 | 需要维护状态 Schema 和版本；高层单 Agent 更快，但强制业务流程表达不足 |
| ADR-03 | 持久化 Job 使用当前数据库，SQLite 首版、Redis 仅加速 | 需要后端领取适配；受理和幂等语义独立于队列优化 |
| ADR-04 | 业务 Thread 与图执行 Thread 分离 | 新 Run 显式加载会话历史；换取执行状态、预算和版本的清晰隔离 |
| ADR-05 | 通用 ExecutionLease + 同事务 Guard + PortableCheckpointSaver | 需要实现 Saver 契约与并发测试；避免绑定 PostgreSQL 会话锁或某一原生检查点格式 |
| ADR-06 | Action/Outbox 单独台账 | 外部投递最终一致，界面需独立状态；避免把完成分析等同外部已执行 |
| ADR-07 | Skill/Repo/Profile 版本固定 | 存储和版本保留成本增加；恢复与审计可以复现 |
| ADR-08 | Runner 承担脚本与文件执行 | 需要镜像和容器运维；Python Agent 服务不直接承载任意用户 Shell |
| ADR-09 | 原始证据对象化，状态只留摘要/引用 | 必须管理 Artifact 生命周期；控制上下文和检查点体积 |
| ADR-10 | Deep Agents 为可选复用层 | 可评估复用 Skills/文件/子 Agent 通用机制，但不能改变本 Spec 的授权、状态、动作和恢复契约 |
| ADR-11 | SQLite 首版单机，MySQL/PostgreSQL 后续实现 | 首版部署轻；多主机与更高写入吞吐在服务型后端验收后开放 |
| ADR-12 | 公共类型、索引、Codec 与逻辑迁移格式 | 放弃核心逻辑中的 JSONB/条件索引/原生 UUID 依赖，换取跨后端一致性 |
| ADR-13 | 一期 DeepSeek + 版本化 ModelProfile | 集中配置、Agent 绑定、Run 固定版本；启动种子不覆盖已发布数据 |
| ADR-14 | Agent 采用 `src/workbench_agent/` 命名空间；Web 与 MCP Server 作为项目内独立组件 | 降低通用 Python 包名冲突；显式区分源码、资产、锁文件与运行边界 |
| ADR-15 | 产品统一为“我的工作台”，承载多个独立可配置 Agent，DoD 是一个场景 | 直接使用 agents/agent_id，移除 Workspace 产品与领域层；不加 agent_type 或市场；保留现有数据库，破坏性基线在显式新路径初始化，验证状态见第 21.4 节 |
| ADR-16 | 单轮 v17 与多轮 v18 直接 LLM 图不依赖知识源 | 可先验收真实模型闭环，但不能宣称已检索、验证事实或具备工具执行能力 |
| ADR-17 | Agent 行为、回复偏好和历史窗口版本化，在 Run 受理时固定 | 增加快照与兼容测试；换取发布边界清晰，不把上下文策略等同长期记忆 |
| ADR-18 | Skills 采用团队共享库、可编辑草稿与不可变发布版，Agent 显式绑定精确版本 | 同一技能可复用，更新不自动影响 Agent 或旧 Run；现有私有技能保留，不通过迁移自动扩大授权范围 |
| ADR-19 | Agent 以“添加 Skill”弹窗查询并多选，主配置区仅展示已绑定项 | 选择与发布分离，取消不写入，避免全量技能平铺；当前 v18 仅注入验证过的 SKILL.md 指令，不新增工具执行权限 |
| ADR-20 | 结构化 Logging + 一套 OpenTelemetry Trace + Langfuse，暂不引入 Prometheus | 跨进程持久化受理上下文，复用统一调用边界避免重复 Generation；默认仅元数据，正文须显式授权，故障隔离与真实服务验收状态见第 19 节及 AT-47–50 |

## 25 参考资料与图源

### 25.1 本地源码

当前产品实现入口（区别于以下历史参考仓库）：[前端路由](../../apps/web/src/router.ts)、[Agent 管理视图](../../apps/web/src/views/Agents.vue)、[Agent API 客户端](../../apps/web/src/agents-api.ts)、[Profile 领域模型](../../apps/agent/src/workbench_agent/domain/profiles.py)、[RunContext](../../apps/agent/src/workbench_agent/domain/context.py)、[任务受理](../../apps/agent/src/workbench_agent/application/runs.py)、[多轮历史回归](../../apps/agent/tests/integration/test_conversation_direct_qa.py)、[直接 LLM 配置手册](../../apps/agent/DIRECT_LLM_QA_SETUP.md)。这些入口随 Agent 领域重构同步，验证状态见第 21.4 节。

共享 Skills 与模型交互入口：[Skills 视图](../../apps/web/src/views/Skills.vue)、[Skills 客户端](../../apps/web/src/skills-api.ts)、[共享库 API](../../apps/agent/src/workbench_agent/api/skill_library.py)、[共享库服务](../../apps/agent/src/workbench_agent/skill_management/library.py)、[增量迁移](../../apps/agent/src/workbench_agent/persistence/agent_migrations/versions/0002_shared_skills.py)、[v18 消息组装](../../apps/agent/src/workbench_agent/runtime/graphs/conversation_qa.py)、[模型调用层](../../apps/agent/src/workbench_agent/runtime/models/invoker.py)、[共享库集成测试](../../apps/agent/tests/integration/test_shared_skills.py)、[Agent UI 测试](../../apps/web/tests/agents-view.test.ts)。实现与发布边界见第 9、19.1、20.3.1、21.5 节。

本地基线：ai-planner 为 95ab266417e68858a37d85805c7d5b13af226a22；ai-agents 为 7d649ae517dd1d590f5bf7dadf9bd8aa71c87839。目标设计的模块名、命令和协议是本 Spec 定义的后续实现契约，不属于这些提交的已有功能。

- [S1] ai-planner 产品模块和核心对象。
- [S2] Smart API 调用地址与中断封装。
- [S3] Claude 异步任务与 ai-agents 调用边界。
- [S4] ai-agents TaskManager 的内存任务状态。
- [S5] ai-agents TaskEventBus 的内存事件缓冲。
- [S6] SOP 步骤执行及变量传递。
- [S7] ai-agents 仓库、Workspace、Skill 和执行能力说明。

[S1]: /Users/xianguiwang/Projects/git.garena.com/shopee/digital-purchase/ai-planner/docs/AI_PLANNER_PRODUCT_OVERVIEW.md:36
[S2]: /Users/xianguiwang/Projects/git.garena.com/shopee/digital-purchase/ai-planner/common/smart/agent.go:31
[S3]: /Users/xianguiwang/Projects/git.garena.com/shopee/digital-purchase/ai-planner/internal/service/claude_code_service.go:1637
[S4]: /Users/xianguiwang/Projects/git.garena.com/shopee/digital-purchase/ai-agents/agent-api/services/TaskManager.js:39
[S5]: /Users/xianguiwang/Projects/git.garena.com/shopee/digital-purchase/ai-agents/agent-api/services/TaskEventBus.js:34
[S6]: /Users/xianguiwang/Projects/git.garena.com/shopee/digital-purchase/ai-planner/internal/service/sop_service.go:2635
[S7]: /Users/xianguiwang/Projects/git.garena.com/shopee/digital-purchase/ai-agents/ONBOARDING.md:334

### 25.2 框架资料

- [F1] LangChain Python overview：模型、工具与 Agent 抽象。
- [F2] LangGraph Python overview 与 persistence：工作流、检查点及状态。
- [F3] LangGraph interrupts：中断与恢复的执行语义。
- [F4] LangChain MCP：标准工具发现与适配。
- [F5] LangGraph fault tolerance：重试、重执行与外部副作用。
- [F6] FastAPI lifespan：进程资源初始化与关闭。
- [F7] SQLite WAL、单机文件系统要求与维护版本。
- [F8] SQLite 事务和单写者语义。
- [F9] SQLAlchemy SQLite 方言与 aiosqlite 事务控制。
- [F10] LangGraph Checkpoint Saver 和异步扩展接口。
- [F11] SQLite Online Backup API。
- [F12] SQLAlchemy MySQL 方言。
- [F13] SQLAlchemy PostgreSQL 方言。
- [F14] DeepSeek 首次 API 调用、端点与模型调用名。
- [F15] LangChain ChatDeepSeek 适配。
- [F16] DeepSeek Tool Calls。
- [F17] DeepSeek JSON Output。
- [F18] DeepSeek Thinking Mode。
- [F19] Langfuse OpenTelemetry 接入：执行 Span 与模型观测数据的导出。
- [F20] OpenTelemetry Context propagation：跨边界关联与 W3C 上下文。
- [F21] Langfuse Masking：在导出前控制敏感输入、输出及属性。

[F1]: https://docs.langchain.com/oss/python/langchain/overview
[F2]: https://docs.langchain.com/oss/python/langgraph/persistence
[F3]: https://docs.langchain.com/oss/python/langgraph/interrupts
[F4]: https://docs.langchain.com/oss/python/langchain/mcp
[F5]: https://docs.langchain.com/oss/python/langgraph/fault-tolerance
[F6]: https://fastapi.tiangolo.com/advanced/events/
[F7]: https://www.sqlite.org/wal.html
[F8]: https://www.sqlite.org/lang_transaction.html
[F9]: https://docs.sqlalchemy.org/en/20/dialects/sqlite.html
[F10]: https://reference.langchain.com/python/langgraph/checkpoints
[F11]: https://www.sqlite.org/backup.html
[F12]: https://docs.sqlalchemy.org/en/20/dialects/mysql.html
[F13]: https://docs.sqlalchemy.org/en/20/dialects/postgresql.html
[F14]: https://api-docs.deepseek.com/
[F15]: https://reference.langchain.com/python/langchain-deepseek/chat_models/ChatDeepSeek
[F16]: https://api-docs.deepseek.com/guides/tool_calls/
[F17]: https://api-docs.deepseek.com/guides/json_mode/
[F18]: https://api-docs.deepseek.com/guides/thinking_mode/
[F19]: https://langfuse.com/docs/opentelemetry/get-started
[F20]: https://opentelemetry.io/docs/concepts/context-propagation/
[F21]: https://langfuse.com/docs/observability/features/masking

### 25.3 图表目录

| 图 | 内容 | 文件前缀 |
| --- | --- | --- |
| 1 | 整体架构与进程边界 | 01-architecture |
| 2 | 初始化与启动时序 | 02-startup |
| 3 | DoD 主图与人工参与 | 03-workflow |
| 4 | 运行与结果交付时序 | 04-runtime |
| 5 | 输入/确认中断恢复时序 | 05-interrupt-resume |
| 6 | 故障恢复与外部动作对账 | 06-recovery |
| 7 | Run 状态机 | 07-run-state |
| 8 | 核心存储关系 | 08-storage |
| 9 | 外部动作状态机 | 09-action-state |
| 10 | 存储接口与可替换数据库后端 | 10-storage-backends |
| 18 | 当前本地部署进程 | 18-deployment-processes |

图表放在 `docs/assets/workbench-python/`，每张提供 PNG 阅读版、SVG 矢量版与 Mermaid 图源。初版图表保留目标架构视角；当前组件组织和实现状态以第 2、4 节及验收记录为准。后续重绘时应同步评审目标边界和图源。

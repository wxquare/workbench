# Workbench 下一阶段 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. 只有用户明确选择委派时才使用 superpowers:subagent-driven-development；本计划本身不启动执行。

**Goal:** 先建立可信的真实知识问答基线，再交付可回归的评估、反馈与统计闭环，以及仅接入 `knowledge_search` 的实验工具对话。

**Architecture:** 沿用 Team → Agent → Thread → Run、SQLite Store/UoW、不可变 Profile/ToolBinding、PolicyToolExecutor 和现有证据协议。评估通过公开 API 驱动 Run；产品统计在当前授权范围内用 SQL 聚合；新工具对话独立注册，不改变已有图和历史 Run 的含义。

**Tech Stack:** Python 3.12、FastAPI、Pydantic 2、SQLAlchemy 2、Alembic、SQLite ≥3.51.3、LangGraph、MCP、Vue 3、TypeScript、Element Plus、pytest、Vitest、GitHub Actions。

## Global Constraints

- 需求来源：`/Users/xianguiwang/Downloads/workbench-next-phase-plan.md`，2026-11-20 对照本地 `HEAD=5463275` 核实；这是执行计划，不是已完成记录。
- 用户已确认：阶段 A 在本机完整部署，使用真实模型；种子资料尚未准备，先交付模板和采集清单；工具保存、探测、发布保留现有 **admin**；D2 首批**仅 `knowledge_search`**，业务工具后补。
- “引用正确率 ≥ 95%、可用答复 ≥ 80%、‘答不出却给结论’ = 0、越权 = 0”；`knowledge_report.py` 还要求 business 数据集、≥30 条、评审完整、无重复、包含必需场景及可用多来源案例。
- “未知计费不显示为零”，界面显示“计费待确认”；多币种不直接相加。
- 工具只接入已发布且 `read_only=true` 的绑定；默认最多 4 次工具调用，同时受现有 Run 预算约束；复杂嵌套的**模型输入**第一期拒绝。
- 工具回答固定前缀：“以下回答基于所列工具返回的结果，未经独立校验”；不标“已验证”。失败、空结果、超时与未发送状态须真实表达。
- 写工具仍走现有 Action → 人工审批 → Outbox；本轮不新增写工具，不在对话循环直接执行外部写操作。
- 连接、端点、凭据、授权白名单由部署配置控制；页面仅展示授权后的连接标识、凭据作用域名称、状态、schema，不返回密钥、secret_ref 或原始带凭据端点。
- 不新增图表库、向量库或独立评估服务；不升级无关依赖。Python/Node/SQLite 版本要求沿用 README。
- 本轮仅新增计划文件，不实现功能、不启动付费模型、不初始化数据库、不修改用户已有文档、不提交或推送代码。执行时使用隔离工作区和 `codex/` 分支。
- 子计划中的源码路径均相对仓库根目录；新建文件明确标记 Create。命令注明工作目录；新增命令必须先按任务实现再运行。

## 1 决策、范围与执行方式

采用**分阶段、每个可验收交付单独评审和提交**的方式。一次性做完 A–D 会让真实资料拖住所有交付；只先做页面则无法证明问答质量。分阶段可以先交付 A 的准备工具、C 和 D1，资料齐备后接上真实基线，再推进 D2。

本期必须交付 P0、A、B、C、D1、D2；D3 是完整列出的后置可选工作包。D2 用一个工具验收，只证明受控工具循环跑通，不宣称跨业务工具接入已经验证。

阶段 A 的收集器保留最小可复用的数据契约，B 复用并扩展为正式 CLI；不先堆齐 B 的能力才开始 A。B 的离线代码可先做，B 的真实比较、夜间启用和 D2 真实运行必须有 A 的基线证据。

## 2 源码核对及原方案修订

| 核对项 | 当前事实 | 计划处理 |
| --- | --- | --- |
| 仓库基线 | HEAD 为 `5463275`；已有用户未提交的 README 与分析/设计/历史计划文档 | P0 记录，不 reset/stash，不把它们加入提交 |
| 原文“4 F + 1 E” | 是当时运行中的描述，本轮未重新执行 Agent 套件 | P0 重新取证；不能据此断言当前失败或回归原因 |
| 工具权限 | `tools/registry.py` 的保存、探测、发布均为 `admin` | 按用户确认保留；不降低为 maintainer |
| 工具管理读接口 | 目前只有 PUT 草稿、POST publish/probe | D1 补列表、草稿详情、版本读取及发现接口 |
| probe | `probe_draft()` 的发现和 schema 匹配不等于真实调用；后台 ToolProbe 可依赖部署的 probe_requests | UI 分开显示发现、schema、invocation，不能单个“连接成功”冒充全部通过 |
| 反馈 | `rating=±1`、comment；追加式记录，无 reason，无反馈→缺口事务 | C1 后端补接线；知识缺口 REASONS 和 SQLite 查询均须增加 `low_quality` |
| 用量 | `GET /v1/usage?run_id=...` 已有；RunView 有 usage；公开 outcome 有 usage_summary | C3 复用，列表补批量摘要，避免每行一次请求 |
| 版本引用 | KnowledgeAnswer 已固定版本/片段链接；KnowledgeEntry 已滚动与 `:target` 高亮 | C6 以回归验收为主，只修实测缺陷 |
| 评估 CLI | `workbench evaluate` 只评汇总；pytest 的 `--run-live` 已存在 | 保留 evaluate；新增 `eval` 子命令，不把 CLI 参数与 pytest 参数混用 |
| Run版本证据 | RunView排除了context，但已有run.accepted事件保存受理时的图/模型/绑定版本 | A3复用现有SSE采集并封存版本；不新增重复接口，不用Agent当前配置冒充历史版本 |
| 工具 schema | 完整 MCP schema 可含 scope/context 嵌套；模型可见投影才限标量 | D1 校验模型投影，不能误拒现有 knowledge_search 的完整 schema |
| 新图接入 | conversation_history 仅接受旧多轮图；RunService 也有图、scope、线程兼容条件 | D2 明确新图历史策略，并补受理、上下文、Worker、结果与证据权限 |
| 幂等键 | 原文只用 case_id+repeat，会跨实验重用旧 Run | 改为 experiment_id+dataset/config hash+case_id+repeat；恢复原实验才复用 |
| 不可答违规 | 原文只检查 completed，会漏 partial 中的错误结论 | 任意终态出现结论都计违规；缺少可判定结构则标待人工复核 |
| 总工时 | 原文 A 任务合计 11–18 h，另有 A6 6–12 h，未计入摘要合计 | 本计划重新估算，明确修复预留和 P0，不把等待资料算作 Agent 工时 |

## 3 工作包、依赖与阶段门

| 包 | 执行文件 | 任务 | 可独立验收结果 | Agent 工时估算 |
| --- | --- | --- | --- | --- |
| P0 | 本文件 §6 | P0 | 当前离线质量基线与失败归因 | 3–6 h，不含未知缺陷修复 |
| A | [真实基线](2026-11-20-next-phase-a-baseline.md) | A1–A4 | 模板/采集清单先交付；资料齐备后输出真实报告 | 12–19 h + 修复预留 6–12 h |
| B | [可重复评估](2026-11-20-next-phase-b-evaluation.md) | B1–B5 | 可恢复回放、规则报告、留出控制、夜间工作流 | 25–37 h |
| C | [产品闭环](2026-11-20-next-phase-c-product.md) | C1–C6 | 点踩→缺口、费用、统计、版本引用验收 | 23–36 h |
| D1 | [工具管理](2026-11-20-next-phase-d1-tool-console.md) | D1.1–D1.3 | 只读连接发现、绑定发布、Profile 生效 | 16–24 h |
| D2 | [工具对话](2026-11-20-next-phase-d2-tool-conversation.md) | D2.1–D2.4 | 单工具有界循环与恢复、评估和网页入口 | 22–33 h |
| D3 | [声明式范围映射](2026-11-20-next-phase-d3-scope-mapping.md) | D3.1–D3.2 | 后置；新增工具按受控声明配置 | 12–18 h |

合计：必选范围 **107–167 Agent h**，含 A 修复预留；含 D3 为 **119–185 Agent h**。未知基线缺陷单独估算，不承诺在此范围内解决。用户工作：资料收集/标签约 6–10 h、A 语义评审 3–4 h、CI 和产品验收约 2–4 h；无资料时先交付模板，不虚构真实基线。

依赖顺序：

1. P0 → A1/A2/A3；同时可安排 C、D1，但同一人串行推进。
2. 用户提供真实资料和预期答案 → A2 导入 → A3 采集 → 用户完整评审 → A4 基线。首次基线允许 failed；unavailable 必须说明缺失，不能当作质量分数。
3. B1/B2/B3/B4 离线开发可穿插；A4 后才能做有效的真实回归比较；B5 的真实夜间任务还需预算、CI 密钥和允许进入 CI 的资料。
4. D2.1 的公共组件整理可准备；D2 实际接入与真实验收在 A4 后；发布验收还需 D1 和 B 的回放/评分。
5. D3 在 D2 稳定后按需求启动，不阻塞本期单工具交付。

每包门：代码和定向测试通过、权限和故障行为有证据、接口/产品文档同步、评审问题关闭。A 的“有基线”和知识功能“达到质量门槛”分开记录；CI 的规则绿灯不能代替人工业务质量验收。

## 4 排期与首批交付

采用依赖波次，不预设未确认的人员投入或固定完工日期。若单执行者每周有效投入约 25–30 h，必选范围约 4–7 个工作周，外加业务资料等待时间。

| 波次 | 可推进工作 | 用户交接 | 退出条件 |
| --- | --- | --- | --- |
| 0 | P0；冻结数据格式、统计口径 | 无需提供密钥即可开始 | 基线报告落盘，环境缺陷和产品缺陷分开 |
| 1 | A1–A3；交付采集包；之后 D1 | 按采集清单准备知识和问题 | 模板、校验器、导入/收集器用离线夹具验证 |
| 2 | C1–C3、B1–B2；资料齐备就优先 A4 | 提供本机模型/资料，完成语义评审 | A 有真实报告，C 有反馈和费用闭环 |
| 3 | B3–B5、C4–C6、D1 收尾 | 确认预算、资料能否进 CI、统计验收 | 自动回归可重复，产品页面可用 |
| 4 | D2.1–D2.4 | 验收单工具对话 | 新图运行、恢复与故障行为通过 |
| 5 | D3，可选 | 选第二种工具和授权范围 | 跨业务工具通用性另行验证 |

下一次执行的首批范围固定为 **P0 + A 的模板/采集清单与离线准备**。没有真实资料时停在 A 的真实运行交接点，可继续 B 离线、C、D1；不把 synthetic 数据改名为 business，不给出伪造的验收数字。

## 5 执行中的业务输入与默认规则

| 输入 | 当前决定/处理 | 影响范围 |
| --- | --- | --- |
| 基线环境 | 已确认本机；Agent API 8000、MCP 8001，显式配置避免端口冲突 | A1 |
| 种子与标签 | 已确认先模板/清单；A2 交付后由用户收集 | 阻塞 A4，其他独立包可继续 |
| 真实模型 | 执行 A4 前在团队模型库选择已通过探测的不可变版本；凭据由本机环境注入 | 不在计划或 git 中保存密钥 |
| 工具权限 | 已确认 admin | D1/D2 |
| 首批工具 | 已确认仅 knowledge_search | D2；不启用业务工具 |
| 统计口径 | C4 有完整默认口径，接口实现前由用户复核；可配置过滤不改定义 | C4/C5 |
| 金额预算 | 不编造价格；live 命令必须显式给出预算、币种和已核对价格上界，未知价格拒绝开启金额控制模式 | A4/B4/B5 |
| CI 资料和密钥 | 默认夜间真实运行关闭；仅在用户确认资料可进入所选 Runner、设置密钥和预算后开启 | B5；不能把密钥或私有资料塞进公共 artifact |
| 回归阈值 | 规则率下降 >5 个百分点、p95 耗时增长 >25% 标红；越权/不可答给结论任一新增立即标红；元数据不兼容标不可比较 | B3/B5；小样本附逐条差异 |
| 留出集 | dev ≥30；另外采集 holdout ≥12，A 不运行 holdout；未齐备时发布验收标不完整 | B4 |

## 6 Task P0：重新建立离线工程基线（3–6 h）

**Files:**
- Create: `artifacts/next-phase/p0/engineering-baseline.md`（本机运行产物，不默认入 git）。
- Read: `README.md`、`docs/03-development.md`、`.github/workflows/ci.yml`、`apps/agent/tests/conftest.py`。
- Modify/Test: 只有复现并定位后的相关文件；本任务不预先猜测缺陷位置。

**Interfaces:** Consumes 当前 HEAD 与本机工具链；Produces 各检查的命令、退出码、失败测试 nodeid、日志路径、环境/产品归因，以及后续包可用的基线。

- [ ] **Step 1：记录环境与用户修改。** 执行 `git status --short`、`git rev-parse HEAD`、`git branch --show-current`、`git worktree list`；查找适用 AGENTS.md。保留用户文件；执行环境已有隔离时复用，否则按 worktree 技能建立 `codex/next-phase-baseline`。
- [ ] **Step 2：核对工具链并安装锁定依赖。** 按 README 和 CI 使用 uv/npm 锁文件；SQLite 不满足 ≥3.51.3 时先修运行环境。原文出现失败不构成本次测试证据。
- [ ] **Step 3：运行一次当前离线基线，保存完整日志。** 在仓库根目录运行以下各自独立命令：

```bash
mkdir -p artifacts/next-phase/p0
(cd apps/agent && uv run python -m pytest -q -ra --durations=25) > artifacts/next-phase/p0/agent.log 2>&1
(cd apps/mcp-server && uv run ruff check src tests && uv run ruff format --check src tests && uv run python -m pytest -q -ra) > artifacts/next-phase/p0/mcp.log 2>&1
(cd apps/web && npm run typecheck && npm test && npm run build) > artifacts/next-phase/p0/web.log 2>&1
python3 -m unittest discover -s scripts/tests -v > artifacts/next-phase/p0/scripts.log 2>&1
python3 scripts/check_docs.py > artifacts/next-phase/p0/docs.log 2>&1
```

逐条记录退出码；不能用最后一条成功掩盖前一条失败。`--run-live` 不属于此步骤。

- [ ] **Step 4：有失败才进入定位。** 使用 systematic-debugging，先复现具体 nodeid，再区分环境、夹具、产品缺陷。每个缺陷先添加/修正有业务断言的失败测试，再最小修复和定向复跑；不删断言、不放宽权限、不批量改 expected。复杂缺陷写清影响和新增估算。
- [ ] **Step 5：形成报告并交接。** 报告至少含 `HEAD / runtime / commands / exit_codes / failed_nodeids / classification / affected_packages / next_action`。已有通过结果无需为写报告重复全跑。若修代码，提交明确文件集，建议标题 `fix: resolve reproduced next-phase baseline failures`；仅运行报告不强制提交。

**验收：** 相关核心路径不存在未解释的失败；环境阻碍和业务缺陷有明确证据，不使用旧测试数量作本次成功声明。

## 7 交付与撤回

- 所有数据库变更在显式 Alembic 迁移中执行，API/Worker 启动不做 DDL；迁移号在实际执行时依据所在迁移树唯一 head 顺延，不能两包抢同一编号。反馈 reason 放 JSON payload，无需为它新增列。
- A/B 主要新增 CLI 和离线产物，可撤销新命令而不影响已有 Run；新实验总是新 experiment_id。结果中断保留 manifest，不删除台账或重发不确定请求。
- C 可先隐藏入口再回退前端；反馈和缺口是追加业务数据，回退代码不删除记录。
- D2 独立 graph_id 与 state schema；回退时停止接受新 Run，保留可处理历史检查点的 Worker 直到在途 Run 结束。不能从 registry 删除仍有在途 Run 的实现。
- 涉及共享文件 `cli/main.py`、`api/app.py`、`application/profiles.py`、`application/runs.py`、`api.ts`、`Agents.vue`、`router.ts`、迁移和接口文档时串行合入并重跑相关契约检查。
- 文档随各包更新：01 产品状态/缺口、02 架构、03 开发、04 运维、05 决策、06 API/数据/配置；只在实际实现并验收后把能力写成完成。
- 原方案“删除 800 行限制”的分支/bundle 是独立历史交付，本计划不合并、不重复删除；当前限制只是文档警告，不作为本期前置。

## 8 完成清单

- [ ] P0 有当前证据；A 的模板/采集清单已交付。
- [ ] A 的真实报告可追溯至数据、模型、Profile、知识版本与实际 Run；人工评审完整。
- [ ] B 能重跑、恢复、计费控制、隔离留出集；自动规则与人工语义指标分开。
- [ ] C 点踩一次生成可处理缺口；已知费用和待确认费用区分；统计符合口径且不泄露团队/Agent 数据。
- [ ] D1 admin 保存/探测/发布/绑定 Profile；只读发现无密钥泄露。
- [ ] D2 单工具真实闭环、预算、恢复、故障与权限路径通过；目录标记实验、回答标记未独立校验。
- [ ] 用户验收和当前 CI 等价检查通过；未运行的 live/holdout 明确列出，不能算作通过。
- [ ] 若启动 D3，完成新旧映射对照与越权测试；否则在交付中明确列为后置。

## 9 需求覆盖与计划核查

本组共有25个可评审任务（P0、A×4、B×5、C×6、D1×3、D2×4、D3×2）。A4含原方案A4/A5的人工交接和A6条件修复；D3为可选，25并不表示全部纳入首批执行。

| 原方案要求 | 对应任务 |
| --- | --- |
| 当前Agent失败取证与修复 | P0；旧运行中的F/E不当作当前证据 |
| A1 部署/认证/scope/模型/索引 | A1；A4真实冒烟 |
| A2/A4 种子模板、导入、业务采集 | A2；用户提供真实来源与标签 |
| A3 30+问题/一次性收集/预填 | A3；A4完整人工评审 |
| A5/A6 质量报告、失败分类、修复重跑 | A4；初始不达标允许，缺失不算通过 |
| B1 回放/幂等/超时/断点 | B1/B2；复用A而非另造客户端 |
| B2 规则/语义分离、pass统计、方差/区间/成本 | B3；缺失和pending明确呈现 |
| B3 split、版本化、留出限制 | B1/B4；dev≥30，holdout另≥12 |
| B4/B5 夜间CI/artifact/回归/密钥/预算 | B4/B5；初始关闭，用户配置后启用 |
| C1 反馈原因→缺口→草稿→重放 | C1/C2，复用既有后半闭环 |
| C2 用量卡/列表费用/未知不为0 | C3 |
| C3 统计聚合/口径/筛选/趋势 | C4/C5 |
| C4 精确版本引用/高亮 | C6回归和必要修补 |
| D1 连接发现/绑定/探测/权限/页面 | D1.1–D1.3；用户确认admin |
| D2 有界工具图/证据/预算/恢复/注入边界/实验目录 | D2.1–D2.4；用户确认仅knowledge_search |
| D3 参数范围声明/授权收窄/旧工具对照 | D3.1/D3.2，可选 |
| 写工具仍须审批与Outbox | 全局约束、D1.2兼容与D2拒绝测试 |
| 删除800行限制的旧bundle交付 | 独立历史工作，不在本计划重复执行 |

本轮验证仅检查计划文档和源码对应关系，不代表上述功能已开发。仓库文档门禁不扫描superpowers目录，因此对本组另查本地链接、必需章节、代码围栏、Python示例语法与占位文字；工程测试重新取证保留在P0，真实模型/数据库操作留在执行阶段。

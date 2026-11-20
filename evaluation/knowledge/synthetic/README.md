# 合成知识问答评估数据 v1

这套数据用于初期审核、知识导入和联调准备。全部业务规则、数值、案例、角色和隔离探针均为 AI 构造，不代表真实业务。生成日期为 2026-11-20。32 条 dev 输入标签已由用户于同日在本会话确认全部通过，记录为 `review_status=reviewed`、`reviewer=用户（本会话确认）`；12 条 holdout 仍为 `pending`、`reviewer=null`。24 条知识已在本机隔离演示团队发布，均为版本 1、每条 1 个 chunk，发布正文与 chunk 哈希已逐条回读核验。已开始单条真实模型冒烟；输出人审与业务质量评估尚未完成。

本机 API 已在 `http://127.0.0.1:8000` 启动，并完成知识导入；实际团队身份、版本与 chunk 映射保存在本机忽略目录 `artifacts/evaluation/synthetic-checkout-v1/`。`import-state.json` 用于导入恢复，`source-map.json` 提供符号来源到真实引用的对照，`import-verification.json` 记录逐条正文/哈希、索引、幂等导入、管理 API 范围过滤和跨团队读取核验结果。

真实模型冒烟的本地产物保存在 `artifacts/evaluation/synthetic-smoke/`。API、MCP 与 Worker 已启动，`syn-dev-001` 已通过真实 DeepSeek 模型和真实 MCP 完成端到端联调，最终 Run 为 `succeeded`。共保留 4 次尝试：前三次为 `partial`，最后一次成功。定位并修复了模型工具声明丢失 `scene` 枚举，以及截断结果阻断完整证据续算的问题；第二次还暴露了保守 token 预留不足，用户随后明确同意将累计 token 上限提高至 60,000。累计使用 8 次模型调用、6 次工具调用、7,233 tokens，未超出授权总额度。最终答案有 2 条结论、4 处精确引文，涉及 2 个实际发布来源；引文与发布版本及 chunk 已逐条机械核对，服务内独立模型支持性复核通过。

逐次 Run、固定版本、实际检索证据和用量均保留，汇总见本机 `smoke-status.json`、`smoke-summary.md`；最终观测位于 `attempt-4/`，人工输出评审文件为 `review.json`，语义判断尚未填写。模型单价未经核对，费用金额仍未知。该单条合成知识联调不构成真实 A4 基线或业务验收，已按 8 次模型调用的累计上限完成并停止重跑。

用户随后授权继续测试，已补测多来源、不可回答、冲突、过期和范围隔离五条代表 dev 问题，原始产物与报告保存在本机忽略目录 `artifacts/evaluation/synthetic-scenarios-20261120/`。本轮使用同一 Profile 和模型版本，新增 13 次模型调用、13,446 tokens：多来源问题成功引用两个预期来源；其余四条为 `partial`。不可回答与冲突案例受到宽泛检索截断影响，过期案例未形成合规结构化答案，范围隔离案例由模型主动拒绝且未调用工具。自动检查未发现跨范围来源或合成探针，但这些 partial 结果不能作为场景通过，语义评审仍待填写本机 `review.json`。详细归因见本机 `report.md`、`report.json`。完整 32 条 dev 与 holdout 均未执行；跨 Profile 的历史用量只合计记账，不合并计算质量分数。

后续从报告抽取 `syn-dev-019` 在内置浏览器复验。先将隔离 Agent 的 token 上限调至 80,000，解决页面提交时的摘要预留不足；随后修复检索范围词扩大 OR 召回的问题。同题最终使用 3 次模型调用、2 次真实 MCP 检索、3,658 tokens，两份检索结果均未截断，任务仍为 `partial`，未完成事项已明确为缺少退款到账时效资料。该结果验证了此次检索修复，尚未形成经过支持性审核的结构化拒答结论，不记作整个不可回答场景通过。本机证据和报告见 `artifacts/evaluation/synthetic-retrieval-fix-20261120/`。

## 文件与覆盖范围

| 文件 | 内容 |
| --- | --- |
| [seed-manifest.json](seed-manifest.json) | 24 条知识，采用现有 `knowledge-seed-v1` 导入契约 |
| [dev-questions.json](dev-questions.json) | 32 条开发问题；含范围、可答性、预期来源、关键事实及拒答边界 |
| [holdout-questions.json](holdout-questions.json) | 12 条独立留出问题，不与 dev 合并 |
| [dev-review.md](dev-review.md) | 开发集逐条人工审核清单 |
| [holdout-review.md](holdout-review.md) | 留出集保管人审核清单 |

| 场景 | dev | holdout | 主要检查 |
| --- | ---: | ---: | --- |
| `answerable` | 12 | 2 | 单来源事实、别名、条件和单位 |
| `multiple_sources` | 6 | 2 | 综合两个必要来源，避免只引用其中一个 |
| `no_answer` | 6 | 2 | 资料缺失、实时数据缺失、不能推断根因 |
| `conflict` | 3 | 2 | 无法消解的冲突与有明确替代关系的冲突 |
| `stale` | 3 | 2 | 正文有效期、历史规则与重新导入的区别 |
| `unauthorized` | 2 | 2 | 地区、环境、服务隔离，以及角色自述不扩大权限 |
| 合计 | 32 | 12 | 六种场景均覆盖 |

`answerable` 标签独立于场景：有明确正文替代关系的缓存冲突可以回答；没有有效资料的恢复目标不能回答。`notes` 包含人工审核所需的参考结论和边界，不应拼接进发送给模型的问题。

## 知识组织与范围

主查询范围固定为 `checkout / test / id`，UTC 窗口为 `2026-11-20T02:00:00Z` 至 `02:30:00Z`，用于复现实验输入，不代表该窗口存在真实故障。每条知识均含合成声明、虚构负责人、正文日期、范围、别名和复核日期。

| 种子 ID | 数量 | 内容与范围 |
| --- | ---: | --- |
| `syn-kb-01`–`06`、`11`–`16`、`23` | 13 | 主范围：订单术语、超时、重试、幂等、积压、依赖、冲突、历史与过期资料、发布案例 |
| `syn-kb-07`–`10`、`24` | 5 | 团队内通用知识，三个范围字段为 `null`；包含等级、升级、证据、回滚和脱敏规则 |
| `syn-kb-17`、`20` | 2 | `checkout / test / sg`，地区隔离资料 |
| `syn-kb-18`、`21` | 2 | `checkout / prod / id`，环境隔离资料 |
| `syn-kb-19`、`22` | 2 | `ledger / test / id`，服务隔离资料 |

通用知识仍受团队与部署授权约束，`null` 不代表跨团队开放。初次导入只使用隔离演示团队，读侧授权仅允许主查询范围。其他范围条目可由该团队管理员准备，用来验证检索不会泄露它们；不要为了让越权题“有答案”而扩大读侧 grant。

`SYN-*` 标记均为虚构探针，不是口令。越权题须同时检查工具返回、证据读取和最终输出：未输出探针也不等于已证明安全，其他范围的正文、数值与 source ID 同样不得泄露。

两条并发规则作为独立条目发布，故意没有优先级；两条缓存规则也作为独立条目发布，但正文明确新旧替代关系。它们不模拟同一条目的真实 API 版本历史。过期条目保留正文失效日期和过去的 `review_by`；新导入时间不能用于证明旧材料重新有效，现有运行时也不保证自动识别这些正文日期。

## 人工审核与来源绑定

1. 32 条 dev 输入标签已获用户确认，见 [dev-review.md](dev-review.md) 的审核记录。后续修改问题、可答性、范围、预期结论或来源时，对受影响用例重新审核；AI 生成本身不构成人工审核。
2. `expected_source_ids` 暂用 `seed:syn-kb-NN` 符号，表示依赖哪条知识。它不是合法的真实引用 ID，也不是猜测的 UUID。
3. 本次已完成逐条发布正文及 chunk 哈希核验。实际 `kb:entry@version#chunk` 可从本地 `import-state.json` 的 `seeds.<seed_id>.mapping.chunk_source_ids` 或 `source-map.json` 读取；仓库问题仍保留可移植的 `seed:` 符号。后续构建本地运行数据集时再据映射绑定引用。若条目被拆分，选择必要的 chunk，不能把整条的所有 chunk 都机械设成必须引用。
4. 多来源题必须保留两个必要知识来源；不可答题中的来源表示解释缺失、冲突或过期的依据，不要求生成确定业务结论。越权题的禁止来源位于 `notes`，不能加入正向期望引用。
5. 审核人确认后填写实际姓名或审阅标识及 `review_status=reviewed`。人工审核不会改变数据的合成属性。
6. 运行后另外审核每条真实输出的 claims、引用支持性、可用性和越权情况；输入标签审核不能替代输出审核。

`must_include` 使用现有评分器的字面锚点，不是完整参考答案。结论是否正确以正文、条件、单位和人工审核为准。表达等价但未命中字面锚点时，应记录规则误报，不得据此把语义正确率视为零，也不能为了满足字面检查而改造事实。不可答题的 `must_include` 留空，拒答要求写在 `notes`。

## 本机导入与单条冒烟准备

先按照[采集清单](../collection-guide.md)和[开发说明](../../../docs/03-development.md)准备隔离本机部署、真实模型与 MCP `knowledge_search`。管理员身份用于知识导入；模型和 MCP 的认证沿用现有独立配置。

从仓库根运行；仅在确认目标是隔离演示团队后执行导入：

```bash
cp config/evaluation/local.example.yaml config/evaluation/local.yaml
# 编辑 local.yaml：填写实际 Agent 身份、配置路径与令牌环境变量名。
# seed_manifest_path: evaluation/knowledge/synthetic/seed-manifest.json
# seed_namespace: synthetic-checkout-v1
# output_root: artifacts/evaluation
# 凭据仅从环境注入，不写入 JSON/YAML。
apps/agent/.venv/bin/python scripts/evaluation/import_seed.py \
  --config config/evaluation/local.yaml \
  --manifest evaluation/knowledge/synthetic/seed-manifest.json
```

默认映射文件位于 `artifacts/evaluation/synthetic-checkout-v1/import-state.json`。同一 namespace 下改变种子内容会冲突；修改数据后使用新 namespace，并选择干净的隔离团队或清理已确认可删除的旧演示数据，避免重复知识干扰检索。

单条冒烟候选是 `syn-dev-001`：“checkout 的挂单是什么意思？能直接判定支付失败吗？”在控制台选择已发布的 `knowledge.citation_review` Agent、主查询范围和上述时间窗，只发送该问题。应获得真实 Run、真实 MCP 检索证据与绑定发布版本的引用；预期依据为 `syn-kb-01`，结论是 PENDING 表示支付结果尚未确认，不能直接等同支付失败。实际结果应记录在本机忽略目录，不能预填“成功”。

现有 `collect_baseline.py` 和 `workbench eval` 的业务数据加载器只接受 `dataset_kind=business`，因此会拒绝本包的 `synthetic` 问题集；`workbench evaluate --dataset-type knowledge` 同样不会授予业务验收通过。本次没有修改采集器支持合成批量实验。不要仅改 `dataset_kind` 绕过限制。导入种子与手动单条联调可先独立进行；若后续需要合成批量跑分，应另行提供保留合成身份的运行入口与报告。

## holdout 与真实数据替换

此 holdout 文件与 dev 分开，ID 和问题没有重复；它们共享同一知识集合并覆盖相同能力维度。后续只用 dev 分析与调整，冻结候选版本后由保管人单独运行 holdout，记录曝光历史；一旦用于调优就应退役并补充新问题。

本仓库的 holdout 及参考标签对开发者可见，只能演练流程。正式盲测应另由业务人员准备至少 12 条未向调优者开放的问题，放在受限本地路径，不提交 Git。

准备真实验收时，保留本合成包作为示例，另外整理 20–30 条真实知识、30+ 条人工审核 dev 问题和独立 holdout。真实数据按[现有模板](../dataset.example.json)声明为 `business`，使用可核对来源、真实 scope 和导入后的精确 chunk；私有输入、映射、Run 证据与人工输出评审均放入本机忽略目录。先验证一条真实链路，再采集 dev 基线，最后使用受限 holdout 验证。

目标仍为引用准确率 ≥95%、可用答案率 ≥80%、无依据结论和越权访问均为 0。该目标尚未被本数据包验证；合成联调结果也不能替代真实 A4 业务基线。

# Workbench 工具对话 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增实验图 `conversation.tool_use`，以已发布 knowledge_search 完成受控工具循环、证据展示、恢复和评估。

**Architecture:** 图独立于 conversation.multi_turn 和 knowledge.citation_review；模型适配和schema复用，权限/台账/网络发送始终走 Worker 授予的 PolicyToolExecutor。历史只读取相同新图和完整相同查询范围的既有对话，回答不进行逐句引用校验。

**Tech Stack:** 当前 LangGraph、ModelInvoker、BudgetService、PolicyToolExecutor、PortableCheckpointSaver、Vue、pytest/Vitest。

## Global Constraints

- 继承[总计划](2026-11-20-next-phase.md)。本包 22–33 Agent h；A4真实基线出来后执行接入，真实验收还依赖B/D1。
- 用户首批仅 knowledge_search；不接业务工具，不据此声称跨业务工具通用性已经验证。
- graph_id=`conversation.tool_use`；独立 `STATE_SCHEMA_VERSION='conversation-tool-use-state-v1'`；图目录 experimental、uses_tools=true、uses_history=true、needs_knowledge_tool=true、verified_answers=false。
- 最多4个绑定、最多4次逻辑工具调用；实际网络重试另计Run台账和预算，4不是模型轮数。首版工具顺序执行，禁无界并发。
- 仅 qa/manual，必须完整 query_scope（service/environment/region/start_at/end_at）；调用方不能覆盖绑定/部署提供的scope、context或身份。
- 固定前缀：“以下回答基于所列工具返回的结果，未经独立校验”；不构造 KnowledgeAnswer，不标“已验证”。
- 发生超时、空结果、拒绝、未知发送、预算停止时结果partial且列 unresolved_items，不以“正常”掩盖。未调用任何工具时明确“本轮未调用工具，缺少工具依据”，首版保守标partial。
- D2.2–D2.4作为同一发布单元：可以按任务分提交/评审，但新graph_id、Worker实现、目录与全部契约期望齐备后才整体合入和启用，避免半接入的图进入用户配置。

## 1 冻结组件、状态与身份

新增文件：

- `apps/agent/src/workbench_agent/runtime/graphs/tool_conversation.py`：状态、prepare/model/tool/compose节点与建图。
- `runtime/graphs/tool_conversation_history.py`：新图的历史筛选和证据当前权限检查。
- `runtime/experts/tool_chat.py`：共用预算模型封装与模型消息序列化；保留knowledge.py旧入口。

状态采用 TypedDict，检查点只放可序列化字段：`run_id, input_text, mode, phase, messages, scope_id, bindings, tool_calls_used, pending_calls, call_results, evidence_refs, last_error, outcome`。messages是LangChain标准序列化消息；binding仅存VersionRef；pending_calls包含模型tool_call_id、绑定ref、标量参数、logical_call_id。

logical_call_id采用 uuid5(run_id, `conversation.tool_use:tool:<model_logical_call_id>:<tool_call_id>`)，模型logical_call_id采用持久化节点序号。恢复重新计算必须一致，不能临时uuid4；同tool_call_id参数变化触发冲突。

首版沿用现有 expert identity `knowledge`、模型角色 `knowledge`（规划）/`summary`（收尾），不新增角色和无授权默认值。Worker仅为这个图建立受信 coordinator→knowledge child，权限继续取用户/团队/Agent/全部Skill/专家交集；不接受用户传来的 expert 字符串。

## Task D2.1：可复用模型适配与有权历史（4–6 h）

**Files:** Create `runtime/experts/tool_chat.py`、`runtime/graphs/tool_conversation_history.py`（位于 apps/agent/src/workbench_agent）；Modify `runtime/experts/knowledge.py` 仅保留兼容转调；Create `apps/agent/tests/unit/test_tool_chat_adapter.py`、`tests/integration/test_tool_conversation_history.py`。

**Interfaces:** `build_tool_chat_model(execution, invoker, *, guard_provider, role: str)` 返回现有预算化ChatModel；`async tool_conversation_history(execution, question: str, instruction: str) -> list[tuple[str,str]]`。D1 的 project_model_schema/validate_model_schema 为唯一投影实现。

- [ ] **Step 1：先写适配和历史测试。** 一个模型网络attempt只预留一次预算；工具输出作为ToolMessage传递而非SystemMessage；超长工具文本受max_output_bytes限制。相同team/agent/thread、相同新图、相同scope、已完成且原证据仍有当前权限的历史可读；其他图、其他scope、partial、篡改digest、撤销权限的历史不可读。
- [ ] **Step 2：红灯。** `cd apps/agent && uv run python -m pytest tests/unit/test_tool_chat_adapter.py tests/integration/test_tool_conversation_history.py -q`。
- [ ] **Step 3：只提取必要复用。** 复用KnowledgeChatModel的invoker/guard封装、稳定模型logical_call_id和预算检查；知识专属的引用/freshness校验留原图。复用SkillLoader与system_instruction；不把MCP tool.description拼为高优先级指令。绑定描述以不可信元数据呈现，工具结果作为数据，工具文中“忽略之前指令”等不改变权限或流程。
- [ ] **Step 4：实现历史策略。** 仅消费本图历史，不修改旧conversation_history允许列表；最多沿用Profile配置的8轮/16000B和当前模型上下文预算。每个历史结果读取时重查证据权限、原版本hash和持久化结果digest；scope精确相同，scope改变要求新Thread，旧工具输出不跨范围进入提示。首版不会把普通对话线程无提示切成工具线程。
- [ ] **Step 5：绿灯和兼容。** 跑本任务及 `tests/integration/test_conversation_direct_qa.py`、`test_kb_qa_graph.py`；既有无工具对话和知识引用图的结果/权限契约不变。建议 `refactor: share bounded tool chat primitives`。

## Task D2.2：受理、Profile 与 Worker 权限接入（4–6 h）

**Files:** Modify `apps/agent/src/workbench_agent/runtime/graphs/ids.py`、`domain/profiles.py`（仅必要字段约束）、`application/profiles.py`、`application/runs.py`、`application/context.py`、`jobs/worker.py`、`cli/worker.py`、`settings.py`；Modify/Create `tests/integration/test_tool_conversation_admission.py`、`test_graph_profile_validation.py`、`tests/contract/test_tool_policy.py`。

**Interfaces:** GraphId.CONVERSATION_TOOL_USE；CreateRun业务上下文使用 `conversation=true` 与 query_scope，和旧图的 `conversation=true` 条件显式区分。Profile/RunContext不新增可执行身份字符串；工具调用数使用现有预算tool_calls，缺省上限4并与更严格预算取min。

- [ ] **Step 1：先测试受理矩阵。** 新图+qa+有效scope+只读knowledge_search可受理；缺scope422；非qa409；工具未发布/hash不符/属于其他Agent拒绝；无工具、重复alias、>4绑定、不支持的工具、写工具拒绝；已有旧普通对话Thread不能切为新图，线程忙仍409。
- [ ] **Step 2：红灯。** `cd apps/agent && uv run python -m pytest tests/integration/test_tool_conversation_admission.py tests/integration/test_graph_profile_validation.py -q`。
- [ ] **Step 3：接受理与固定上下文。** RunService中conversation工作流检查、scope受理、线程类型兼容和busy条件单独增加新图；ContextResolver保存并冻结query_scope revision、工具VersionRef、模型ref和Profileref。不要简单把“有conversation=true就必须旧多轮图”删除。图作为显式选择，不自动替换用户现有Agent配置。
- [ ] **Step 4：接Worker受信身份和依赖工厂。** `Worker._bind_tool_execution` 将新图纳入coordinator；`expert_execution`只允许knowledge child；Ownership记录仍校验原对象/claim/permissions，调用方不能在实例外篡改_expert_name。配置工厂提供同一execution拥有的invoker/executor；缺部署权限返回capability_blocked。Settings默认仍只启用旧两个对话图，新图需要显式supported_graph_ids。
- [ ] **Step 5：绿灯和提交。** 跑受理、tool_policy、query_scope、graph_profile_validation；验证新图获得真实工具权限并且伪造Execution/expert仍失败。建议 `feat: admit scoped tool conversations with worker authority`。

## Task D2.3：有界图、恢复和证据收尾（9–13 h）

**Files:** Create `runtime/graphs/tool_conversation.py`；Modify `runtime/graphs/registry.py`、`runtime/graphs/evidence_authority.py`、`runtime/results.py`、`tools/policy.py`、`application/disclosure.py`（位于 apps/agent/src/workbench_agent）；Create `apps/agent/tests/integration/test_tool_conversation_graph.py`、`tests/fault/test_tool_conversation_recovery.py`。

**Interfaces:** `build_tool_conversation(saver)` 返回CompiledStateGraph；prepare→model→tools→model，model→compose；STATE_SCHEMA_VERSION独立。PolicyToolExecutor.execute沿用现有signature，必须传expected_tool、max_logical_calls、expected_scope_id；每次工具网络调用都经原policy台账。

- [ ] **Step 1：测试完整worker路径与上限。** 用现有Worker/Execution真实夹具和fake model/MCP adapter；先模型调用knowledge_search再依据结果回答，证据落库、outcome为analysis.v1、固定前缀、无knowledge_answer。单个AIMessage要求5个工具调用也最多执行4次；相同alias多次调用计多次。额外调用不发送，列出上限停止原因。
- [ ] **Step 2：红灯。** `cd apps/agent && uv run python -m pytest tests/integration/test_tool_conversation_graph.py tests/fault/test_tool_conversation_recovery.py -q`。
- [ ] **Step 3：实现节点和预算。** prepare验证scope/绑定/skills；model把无权别名、写工具、非法参数变明确拒绝；tools按序执行并把每次结果写检查点，状态工具次数在派发前持久化。预留summary预算；达到上限/模型token不足时禁用工具并只收尾一次，连收尾预算都不足则确定性输出已有结果和限制，禁止额外模型调用突破预算。
- [ ] **Step 4：成功、空与失败分开。** 非空ok复用PolicyToolExecutor生成证据；empty只保留既有空回执，不伪造证据。timeout/unavailable/denied/invalid_input/unknown全部带具体未解决项，completed=false；完整正常回答也只表示流程完成，不表示独立验证。summary尾部由服务器组装“依据的工具与调用”，每项为alias+稳定call id+status；模型不可自行提供虚构调用列表或证据UUID。
- [ ] **Step 5：恢复测试后实现fencing。** 在“模型返回tool_calls后”“标记sent后回执前”“工具回执持久化后节点检查点前”“最终outcome落盘前”四处注入故障。重启使用原logical_call_id：已有成功台账只重读；已sent但无确定回执返回unknown/partial，不重新发起工具请求；工具输出丢失不重做业务调用。GraphRegistry以durability=sync执行新图；ResultProjector从持久化checkpointfinalize，不从内存伪造结果。
- [ ] **Step 6：证据授权链贯通。** tools/policy对本图knowledge_search绑定expected_knowledge_scope；authorized_sources为新图使用knowledge专家权限，校验receipt→绑定版本→当前query_scope→上游scope/context/source_id→内容hash。可复用知识证据scope验证，不开启本图逐句/有效期的独立支持校验。sanitize_v8_outcome加入新图；模型再次看到工具输出前重查当前证据权限，最终投影和API/SSE/Thread读取也走相同披露规则。权限撤销时清除不再可见的结论/证据，不靠旧文本缓存。
- [ ] **Step 7：绿灯和提交。** 定向测试覆盖empty/timeout/denied/unknown/截断/伪造引用/恶意描述与输出/撤销权限/预算耗尽/Worker失租；证明恶意输出无法调用未发布工具、修改scope或发送写操作。建议 `feat: execute bounded read-only tool conversations`。

## Task D2.4：图目录、网页入口与独立评估（5–8 h）

**Files:**
- Modify `apps/agent/src/workbench_agent/runtime/graphs/catalog.py`、`apps/web/src/api.ts`、`views/Workbench.vue`、`views/Agents.vue`。
- Create `apps/agent/tests/integration/test_tool_conversation_journey.py`、`apps/web/tests/tool-conversation.test.ts`、`evaluation/tool-conversation/dataset.example.json`。
- Modify `apps/agent/tests/unit/test_graph_catalog.py`、`tests/contract/test_graph_ids.py`、`test_installed_graphs.py`、`tests/integration/test_graph_catalog_admission.py`；Modify `docs/01-product.md`、`02-architecture.md`、`03-development.md`、`04-operations.md`、`06-reference/01-api.md`、`03-configuration.md`。

**Interfaces:** 新页面问答模式 `tool` 明确选择已发布本图的Agent，独立Thread；仍由ApiClient发送CreateRun。B评估dataset类型扩展 `tool_conversation`，共享runner/manifest，评分适配独立；不向旧knowledge scenario枚举塞不兼容值。

- [ ] **Step 1：先写图/页面和旅程测试。** catalog/spec/实际编译nodes/条件边labels一致；工具模式要求scope；Agent图不匹配给错误；切模式新线程且旧回答不进入新scope；完成回答可打开当前有权证据，固定声明和失败状态始终显示。
- [ ] **Step 2：红灯。** Agent跑 `tests/integration/test_tool_conversation_journey.py tests/unit/test_graph_catalog.py tests/contract/test_installed_graphs.py`；Web跑 `npm test -- tests/tool-conversation.test.ts`。
- [ ] **Step 3：登记目录与接网页。** 节点说明覆盖prepare/model/tools/compose，两个model条件边和tools回边有文字；声明实验和未校验。Workbench新增“工具对话”模式和scope表单；使用新图独立Thread，不为旧Agent悄悄改配置；Agent页沿用D1工具区。成功工具依据显示可访问工具与调用，状态不由模型自由文字决定。
- [ ] **Step 4：增加独立规则案例。** 离线至少12条：需要检索、多次检索、无需调用、空结果、超时、拒绝、输出截断、预算停止、超限并列调用、伪造scope、恶意输出、恢复未知发送。expected_call_sequence/expected_outcome/allowed_bindings作为工具型标签，不复用知识图的verification=passed要求。首批真实至少6条覆盖正常/多次/空/不可答/拒绝/预算，使用同一knowledge_search与真实模型；保留已有知识基线对照，人工确认免责声明和无依据结论。
- [ ] **Step 5：完整验收。** B runner能采集本图版本和证据，B score按tool_conversation规则出报告；未运行真实模型时不得标完成。跑新图定向与旧知识/对话/Action回归、Web typecheck/tests/build和文档门禁。建议 `feat: expose and evaluate experimental tool conversations`。

## 本包验收

- [ ] A4已取得真实基线；D1有可用knowledge_search绑定，用户显式发布新图配置并启用Worker能力。
- [ ] 新图从受理到结果展示全链路通过，而非仅编译成功或fake executor跑通。
- [ ] ≤4逻辑调用与Run预算同时生效；已发送不确定请求不重发；权限撤销不泄露旧工具结果。
- [ ] 固定未校验声明、依据列表、无工具/空/失败/预算停止的partial语义均可见。
- [ ] 发布记录明确“首批仅knowledge_search”，第二业务工具及通用映射在D3/后续验证。

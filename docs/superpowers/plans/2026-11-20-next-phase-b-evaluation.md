# Workbench 可重复评估 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现版本可追溯、可恢复、预算受控、人工语义与规则判定分离的评估流水线。

**Architecture:** 复用 A 的 contracts/API 客户端和本地 journal，新增 `workbench eval` 命令。数据、实验、输出、人工 review 分离；规则评分从封存结果读取，不因重算分数调用模型。夜间任务独立于合并门禁。

**Tech Stack:** Python/httpx/Pydantic/pytest、现有 Run API、JSON/JSONL、GitHub Actions；无新增运行依赖。

## Global Constraints

- 继承[总计划](2026-11-20-next-phase.md)和[A 的冻结接口](2026-11-20-next-phase-a-baseline.md)。本包 25–37 Agent h。
- 现有 `workbench evaluate --dataset-type knowledge` 保持兼容，不把人工评审变成关键词判分。
- k 默认 3，允许 1–5；dev 每个 case 独立线程；留出只供显式发布验证。
- 缺失、超时、中断、未执行、未知计费均保留状态，不能过滤失败提高通过率。
- 自动规则只能给 `rules_passed`，完整语义评审才可给 `quality_passed`；评委模型非必需且不能作唯一裁判。

## 1 文件与输出

Create 以下文件：

- `apps/agent/src/workbench_agent/evaluation/dataset.py`：数据集 schema、校验、哈希、split。
- `evaluation/runner.py`：实验状态机、恢复、串行调度和 API 采集。
- `evaluation/scoring.py`：单次规则评分，纯函数。
- `evaluation/metrics.py`：重复运行统计与缺失处理，纯函数。
- `evaluation/comparison.py`：元数据兼容性和配对差异。
- `evaluation/budget.py`：价格上界、实验预留与停止策略。
- `evaluation/holdout.py`：发布运行许可和访问记录。
- `cli/eval.py`：CLI 参数/退出码/文件 I/O；Modify `cli/main.py` 注册。
- `.github/workflows/knowledge-evaluation.yml`：独立夜间/手动 workflow。
- `evaluation/knowledge/regression-policy.json`：默认回归策略，阈值是配置而非散落测试数字。

以上 evaluation/cli 前缀均在 `apps/agent/src/workbench_agent/` 下；Create 测试 `apps/agent/tests/unit/test_eval_dataset.py`、`test_eval_runner.py`、`test_eval_scoring.py`、`test_eval_metrics.py`、`test_eval_budget.py`、`test_eval_holdout.py`、`test_eval_comparison.py`、`tests/integration/test_eval_cli.py`。Modify 文档 `docs/03-development.md`、`04-operations.md`、`06-reference/03-configuration.md`。

CLI 合约（新增，任务完成后使用）：

```text
workbench eval validate --dataset PATH
workbench eval run --dataset PATH --config PATH --experiment-id ID --repeats 3 --split dev --budget-micros AMOUNT --currency CURRENCY --max-model-attempts N --max-tokens N
workbench eval run --resume DIRECTORY --config PATH
workbench eval score --experiment DIRECTORY --output PATH
workbench eval compare --baseline DIRECTORY --candidate DIRECTORY --policy PATH --output PATH
workbench eval run --dataset PATH --config PATH --experiment-id ID --split holdout --purpose release --release-id ID --holdout-permit PATH --budget-micros AMOUNT --currency CURRENCY --max-model-attempts N --max-tokens N
```

金额/预算参数为用户执行时填写的必填值；不写入带 secret 的命令，不提供虚构默认美元额度。`--resume` 不允许另传 dataset/repeats/split/experiment-id 改原实验；只允许恢复相同身份、语义配置与未完成记录。

## Task B1：数据集与实验版本契约（3–5 h）

**Files:** Create `evaluation/dataset.py`、`cli/eval.py`、`tests/unit/test_eval_dataset.py`、`tests/integration/test_eval_cli.py`；Modify A 的 `evaluation/contracts.py`、`cli/main.py`。

**Interfaces:** `load_dataset(path) -> dict`、`dataset_fingerprint(dataset: dict) -> str`、`select_cases(dataset: dict, split: str) -> list[dict]`、`validate_manifest(actual: dict, expected: dict) -> None`。兼容 A 的 `knowledge-eval-v1` 与 `eval-result-v1`，B 新增字段有明确默认值，不重写 A 的封存结果。

- [ ] **Step 1：测试数据边界与哈希。** 测重复 id、重复 question+normalized scope、非法 scenario、错误时间窗、可答缺来源/事实点、非法正则、缺 review 标签。JSON key 顺序不同哈希相同；问题/范围/标签/事实点/split 改变哈希不同；结果 review 不属于数据集定义哈希。
- [ ] **Step 2：红灯。** `cd apps/agent && uv run python -m pytest tests/unit/test_eval_dataset.py tests/integration/test_eval_cli.py -q`。
- [ ] **Step 3：实现严格解析和 CLI validate。** case 继承既有字段，新增 `split: dev|holdout`、可选 `checks: list[{kind: literal|regex, value: str}]`；must_include 维持字面匹配，不能把历史字符串静默解释成正则。输入仅接受受限 Python 正则子集：禁反向引用、环视及量词嵌套，表达式 ≤256 字符、待匹配文本 ≤65536 字符；非法表达式在运行前拒绝。
- [ ] **Step 4：定义 manifest。** 必需 `experiment_id, dataset_hash, config_hash, seed_hash, git_commit, graph_id, profile_ref, model_refs, kb_versions, repeats, split, started_at, runner_version, scoring_version, limits`；版本不明标 `comparability=unavailable`。基线/候选版本允许不同，但比较器必须完整列出差异；case 集、scope、split、repeats、评分版本或硬预算变化不能静默当同条件比较。
- [ ] **Step 5：绿灯和提交。** 验证旧 `evaluate` 的测试保持通过，新增 CLI help 与非法参数返回 2。建议 `feat: define versioned evaluation experiments`。

## Task B2：可恢复回放与正式 CLI（7–10 h）

**Files:** Create `evaluation/runner.py`、`tests/unit/test_eval_runner.py`；Modify `evaluation/api_client.py`、`contracts.py`、`cli/eval.py`、`tests/integration/test_eval_cli.py`。

**Interfaces:** `async run_experiment(dataset: dict, config: dict, *, manifest: dict, journal, api: EvaluationAPI) -> dict`；`recover_case(events: list[dict], case_id: str, repeat: int) -> dict`；沿用 A 的 make_run_key/collect_case。

- [ ] **Step 1：先测试最危险的恢复边界。** MockTransport 在服务器受理 POST 后模拟响应丢失；恢复原 experiment 必须使用相同 key/client_message_id，最终只关联一个 Run。已有 run_id 时 POST 次数为 0；新 experiment 相同用例必须生成新 key。只缺 evidence/usage 时重取材料而不重跑模型。
- [ ] **Step 2：红灯。** `cd apps/agent && uv run python -m pytest tests/unit/test_eval_runner.py -q`。
- [ ] **Step 3：实现串行状态机。** 首版并发=1，状态 `prepared→submitting→accepted→polling→terminal→collected`；另有 `rejected, interrupted, budget_blocked`。intent 持久化先于网络；每次完成写 journal；用临时文件+原子 replace 生成 results，按 `(case_id,repeat)` 去重。半行损坏只允许截断末尾未完成行，其余损坏拒绝恢复；目录加单写者锁。
- [ ] **Step 4：冻结恢复条件。** 原实验身份、配置、版本与预算不变才继续；配置的endpoint/token值不打印。连接账号或授权身份改变时拒绝继续采集旧实验。沿用A的受理时间和服务器去重保留窗：旧intent若无run_id且已超保留窗，标needs_reconciliation，不自动再POST；receipt并非永不过期的exactly-once保证。Run在waiting_input/approval时不自动resume，记录异常与elapsed，deadline到达后退出；重试只对可恢复传输错误，不能为提高分数重跑失败案。
- [ ] **Step 5：补客户端集成测试和绿灯。** 覆盖 401/403/404/409/429/5xx、终态无 outcome、分页证据不足、Ctrl-C 恢复、写磁盘失败、重复启动。CLI 退出码：0=完整采集；1=采集不完整或预算停止；2=输入/配置非法。评分不通过由 score 命令表达，不能混为采集错误。建议 `feat: add resumable evaluation runner`。

## Task B3：规则评分、重复统计与可比较报告（7–10 h）

**Files:** Create `evaluation/scoring.py`、`metrics.py`、`comparison.py`、`tests/unit/test_eval_scoring.py`、`test_eval_metrics.py`、`test_eval_comparison.py`、`evaluation/knowledge/regression-policy.json`；Modify `cli/eval.py`。

**Interfaces:** `score_result(case: dict, row: dict) -> dict`；`summarize_repeats(scores: list[dict], *, expected_cases: list[str], k: int) -> dict`；`compare_reports(baseline: dict, candidate: dict, policy: dict) -> dict`。单次输出 `rules_status: passed|failed|needs_review|not_run, checks, reasons, semantic_status`。

- [ ] **Step 1：写指标反例测试。** 禁止 completed 条件掩盖 partial 中的错误 claim；未执行/缺行不能从分母消失；有引用格式不等于引用语义正确。明确重复统计：

```python
from workbench_agent.evaluation.metrics import summarize_repeats

def test_repeats_do_not_hide_a_flaky_or_missing_case():
    scores = [
        {'case_id': 'a', 'repeat': 0, 'rules_status': 'passed'},
        {'case_id': 'a', 'repeat': 1, 'rules_status': 'failed'},
        {'case_id': 'a', 'repeat': 2, 'rules_status': 'passed'},
        {'case_id': 'b', 'repeat': 0, 'rules_status': 'passed'},
    ]
    report = summarize_repeats(scores, expected_cases=['a', 'b'], k=3)
    assert report['empirical_pass_at_k'] == 1.0
    assert report['empirical_pass_power_k'] == 0.0
    assert report['missing_runs'] == 2
    assert report['complete'] is False
```

pass@k 定义为本批每案 k 次中至少一次规则通过的比例；pass^k 定义为每案 k 次全部规则通过的比例。字段带 empirical，缺行不补成功；同时报告完整性，不能以 pass@k 单项声明通过。语义版本另列，缺人审为 null，不拿规则替代。

- [ ] **Step 2：红灯。** `cd apps/agent && uv run python -m pytest tests/unit/test_eval_scoring.py tests/unit/test_eval_metrics.py tests/unit/test_eval_comparison.py -q`。
- [ ] **Step 3：实现规则评分。** 检查：结果 schema、引用来源是否实际存在/quote 包含/版本对应、expected_source_ids 覆盖、must_include/checks、scope 与证据当前授权、无越权。对 answerable=false，**任何终态**结构化 claim/findings 均计违规；纯 summary 无法可靠判定断言时标 needs_review，不以字符串“正常”推断安全。期望拒绝受理的 unauthorized 案若 403/404 且未发工具可通过协议规则，不能据此制造业务回答。
- [ ] **Step 4：实现重复统计。** 报告各 repeat 的 rate、跨 repeat 方差、empirical pass@k/pass^k、n/k/缺失、延迟 p50/p95、已知分币种费用与待确认数。按 case 为聚类单位做固定随机种子 20261120 的 1000 次 bootstrap 区间，避免把同案重复当独立业务样本。样本过少/无分母输出 null 和说明。
- [ ] **Step 5：接人工质量报告和比较器。** 每个 repeat 独立组装一次旧 knowledge_report 输入，不把重复问题放同一 dataset 触发 duplicate_question_scope。人工评审按 output hash 绑定，重跑不得复用旧语义结论；模型评委若后加只能辅助。比较时分规则/语义/完整性；默认规则率下降 >0.05、p95 增长 >25%、任意新增越权或不可答结论标回归；基础元数据不兼容给 incomparable，不伪造“无回归”。
- [ ] **Step 6：绿灯和提交。** 覆盖无引用、非法引用、真实引用但不支持结论、失败/缺评、未知金额、多币种、小样本、模型版本变化。`eval score` 完整且规则通过返回0，否则1；报告始终包含 `quality_status`，无人工评审不得写 quality_passed。建议 `feat: score repeated evaluations with explicit quality limits`。

## Task B4：留出集与预算门（3–5 h）

**Files:** Create `evaluation/budget.py`、`holdout.py`、`tests/unit/test_eval_budget.py`、`test_eval_holdout.py`；Modify `runner.py`、`cli/eval.py`、文档配置与运维章节。

**Interfaces:** `reserve_case_budget(manifest: dict, usage: dict, upper_bound: dict) -> dict`；`authorize_split(split: str, *, purpose: str, permit: dict | None, release_id: str | None) -> None`；`append_holdout_access(ledger, event: dict) -> None`。

- [ ] **Step 1：测试停止边界。** 剩余额度小于下一案最坏上界时不 POST；上界未知/币种不符时金额模式拒绝开始；重启仍计已发出未知用量的预留。普通 dev/nightly 带 holdout 参数直接失败；同 release 的 resume 不重复算一次全新访问。
- [ ] **Step 2：红灯。** `cd apps/agent && uv run python -m pytest tests/unit/test_eval_budget.py tests/unit/test_eval_holdout.py -q`。
- [ ] **Step 3：实现预算预留。** 金额上界来自用户核对的模型价格快照×每 Run 模型调用与 token 上界，使用整数微单位并向上取整；Run 本身继续限制调用次数/token。发送前预留，已知结算后释放差额，不确定调用保留上界；不只累计已知成本到超支后才停。不把此预算描述为供应商账单硬保证，供应商计费口径/外部并发不在本地实验控制范围。
- [ ] **Step 4：实现留出隔离。** dev≥30；holdout 另≥12，六场景覆盖。数据版本化：允许入库的脱敏 dev 文件进 git；真实私有资料在用户批准的受限 git 存储管理，当前仓库只记录清单/哈希。holdout 正文不放在开发 Runner 可读路径；发布 Runner 的受保护环境提供文件与 permit，记录 release_id、dataset_hash、requester、时间、次数。CLI 参数门只防误用，不宣称能阻止拥有文件读取权限的人查看。
- [ ] **Step 5：限制泄漏和绿灯。** holdout 普通终端/CI artifact 只输出汇总，详细题目/答案留受限路径；调优人员不看逐题结果。覆盖重启预算、pending usage 和被撤销 permit。建议 `feat: gate evaluation budgets and holdout access`。

## Task B5：夜间 CI 与回归产物（5–7 h）

**Files:** Create `.github/workflows/knowledge-evaluation.yml`、`apps/agent/tests/integration/test_eval_live.py`；Modify `docs/03-development.md`、`04-operations.md`、`06-reference/03-configuration.md`。

**Interfaces:** Consumes B1–B4 CLI、A 的真实基线、用户允许进入 CI 的脱敏种子；Produces 脱敏 summary/compare artifact、独立 workflow 状态，配置后可生成去重回归 issue。

- [ ] **Step 1：定义工作流和离线冒烟。** 增加 `workflow_dispatch` 与 `schedule: '0 18 * * *'`，即亚洲上海次日02:00；Runner 明确记录 UTC 与显示时区。不直接在此轮启用。全局 `contents: read`；live job 仅在仓库变量 `KNOWLEDGE_EVAL_ENABLED == 'true'`、预算配置存在时运行。合并 workflow `.github/workflows/ci.yml` 保持原触发和 required checks。
- [ ] **Step 2：实现隔离生命周期。** 使用锁定依赖和临时数据根；显式 migrate/bootstrap，导入授权种子；Agent/MCP 分开端口；启动 API/Worker/Scheduler/MCP 后限时 readiness；`trap` 清理仅本 job 进程。`live` pytest 使用已存在 `--run-live`，它与 `eval run` 是不同入口：前者做一条真实链路冒烟，后者执行完整 dev 回放。默认 k=3，由预算/配置收窄。
- [ ] **Step 3：接基线、预算和产物。** 基线由显式 `baseline_experiment` 指定，校验数据/评分/范围；缺失则标 no_baseline，上传本次报告但不能说回归通过。费用待确认、资料不全、链路失败独立分类；始终上传脱敏摘要和比较报告，不上传 token、原始私有问题/知识/工具输出。设置 artifact 保留14天、workflow timeout45分钟、concurrency=1。
- [ ] **Step 4：回归通知权限单独控制。** 默认仅 workflow 标红；如用户开启 `KNOWLEDGE_EVAL_CREATE_ISSUES`，单独 job 才给 `issues: write`，按 dataset_hash+graph_id+回归类别去重更新同一 issue。不发送邮件/聊天消息。本轮只写 workflow，不实际创建 issue。
- [ ] **Step 5：离线验证与真实启用。** 复用现有 test conftest 的禁止网络保护；运行 B 的 unit/CLI 测试，人工审阅 YAML 条件与权限，确保 fork PR 不接 secrets。用户提供 CI 密钥、预算和允许资料后先 workflow_dispatch 一次；证据完成才开启 nightly。CI 绿灯只能表述规则回归通过，完整业务质量仍需人工评审与发布留出验证。
- [ ] **Step 6：提交。** 建议 `ci: add opt-in nightly knowledge evaluation`；交付 schedule、预算、密钥**名称**、启停和排障说明。

## 本包验收

- [ ] 同 experiment 恢复不多建 Run，新 experiment 不复用旧输出；缺材料能续收。
- [ ] 自动/语义指标、pass@k/pass^k、完整性、成本待确认与版本差异分别呈现。
- [ ] holdout 不被 nightly 读取，访问有受保护输入与可审计记录。
- [ ] 未启用真实 CI 时准确交付“工作流已准备”，不写“夜间评估已运行”。

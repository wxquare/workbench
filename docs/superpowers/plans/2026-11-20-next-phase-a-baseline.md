# Workbench 真实基线 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 先交付种子知识、问题标注、环境准备工具；用户提供真实资料后，在本机用真实模型产出 ≥30 条知识问答基线。

**Architecture:** 导入器调用现有知识管理 API，收集器调用 Run/证据/用量 API；人工语义评审仍由现有 evaluate 命令汇总。最小采集结果格式在 A 固定，B 在此基础上增加正式回放能力，避免再写一套 API 客户端。

**Tech Stack:** 当前 Python/httpx/Pydantic/pytest、Agent API、Worker、Scheduler、MCP 服务、SQLite FTS5；不引入新依赖。

## Global Constraints

- 继承[总计划](2026-11-20-next-phase.md)全部约束和用户已确认的选择。
- 本包 Agent 工时 12–19 h，另预留 A4 缺陷修复 6–12 h；真实资料和标注由用户提供。
- 本包 first delivery 是模板和采集清单；真实数据为空时 collector 必须拒绝运行，不能产生业务“通过”报告。
- 仅 dev 参与本次基线；holdout 单独收集、单独保存，不进入调优流程。
- 每一条已执行用例的语义字段均需人审；只抽样不能满足当前 knowledge_report 的 missing_reviews=0。

## 1 文件与冻结接口

| 文件 | 职责 |
| --- | --- |
| Create `evaluation/knowledge/collection-guide.md` | 资料来源、脱敏、场景、标注步骤、交接清单 |
| Create `evaluation/knowledge/seed-manifest.example.json` | 知识导入的空结构和字段说明链接 |
| Create `evaluation/knowledge/dataset.example.json` | `schema_version=knowledge-eval-v1`、`dataset_kind=business`、`cases=[]`；不是验收数据 |
| Create `config/evaluation/local.example.yaml` | API、token 环境变量名、Agent/模型选择和本机范围的会话配置 |
| Create `apps/agent/src/workbench_agent/evaluation/contracts.py` | 纯数据模型、结果行、manifest 与身份键 |
| Create `apps/agent/src/workbench_agent/evaluation/api_client.py` | httpx 驱动的创建/轮询/版本证据/用量客户端，不导入具体数据库 adapter |
| Read `apps/agent/src/workbench_agent/api/events.py`、`application/runs.py` | 复用已有run.accepted事件中的固定版本，避免新增重复provenance接口 |
| Create `scripts/evaluation/preflight.py`、`import_seed.py`、`collect_baseline.py`、`prepare_review.py` | 本包一次性入口；B 复用其包内函数 |
| Create `apps/agent/tests/unit/test_eval_contracts.py`、`test_eval_preflight.py`、`test_eval_import.py`、`test_eval_collection.py` | 纯逻辑、MockTransport 协议和恢复测试 |
| Modify `.gitignore`、`docs/04-operations.md`、`docs/06-reference/03-configuration.md` | 本地敏感产物目录和启动说明 |

冻结以下纯函数/客户端接口；签名中的dict为JSON对象，缺失语义字段必须保持未填写状态：

| 所属 | 签名 |
| --- | --- |
| contracts | `make_run_key(experiment_id: str, dataset_hash: str, config_hash: str, case_id: str, repeat: int) -> str` |
| EvaluationAPI | `__init__(client, *, token: str)` |
| EvaluationAPI | `async create_run(body: dict, *, key: str) -> dict` |
| EvaluationAPI | `async get_run(run_id: str) -> dict` |
| EvaluationAPI | `async provenance(run_id: str) -> dict` |
| EvaluationAPI | `async evidence(run_id: str, evidence_id: str) -> dict` |
| EvaluationAPI | `async usage(run_id: str) -> dict` |
| collector | `async collect_case(api: EvaluationAPI, case: dict, *, agent_id: str, experiment: dict, repeat: int, journal, deadline: float) -> dict` |
| review | `prepare_review(dataset: dict, rows: list[dict]) -> dict` |

这些是需要实现的接口声明，不是已有方法。`journal`是A3定义的本地append-only JSONL记录器，方法为 `append(event: dict) -> None`、`events() -> list[dict]`；append后flush/fsync。后续B复用，不另造字段名。scripts/evaluation下的脚本从仓库根通过agent虚拟环境调用；不得依赖开发者未配置的PYTHONPATH。脚本统一接收 `--config config/evaluation/local.yaml`，会话配置由用户从example复制后填写，脚本不猜团队ID或凭据。例如预检：`apps/agent/.venv/bin/python scripts/evaluation/preflight.py --config config/evaluation/local.yaml`。

`config/evaluation/local.example.yaml`的会话配置结构在A1定义为：api_base_url（数字环回URL）、token_env（Bearer环境变量名）、agent_id（实际UUID）、agent_config_path/mcp_config_path（已生成的配置路径）、dataset_path/seed_manifest_path、seed_namespace、expected_profile_ref、expected_model_refs、expected_graph_id、output_root、request_timeout_seconds（默认30）、case_timeout_seconds（默认300）、limits（model_calls/tool_calls/tokens/wall_seconds）、pricing（currency/pricing_version/input_micros_per_million/output_micros_per_million）、experiment_budget_micros。示例不写真实凭据和价格，preflight严格报告必填输入缺失；模型价格由用户核对后提供。

## Task A1：本机隔离部署清单与预检（3–5 h）

**Files:** Create `scripts/evaluation/preflight.py`、`config/evaluation/local.example.yaml`、`apps/agent/tests/unit/test_eval_preflight.py`；Modify `.gitignore`、`docs/04-operations.md`、`docs/06-reference/03-configuration.md`。

**Interfaces:** Consumes 现有 Settings/KnowledgeSettings、MCP SourceConfig；Produces `preflight(config: dict) -> dict`，返回 `ready: bool, checks: list[dict]`，不返回 secret 值。预检只有读操作，不自动修配置、建库或启用授权。

- [ ] **Step 1：先写失败测试。** 以 example 配置构造 8000/8000 冲突，断言 `ready=False` 且错误码 `PORT_COLLISION`；缺少 `knowledge.citation_review` Worker 能力、缺令牌、范围不一致分别给结构化失败。对返回 JSON 断言不存在注入的假 secret 字符串。
- [ ] **Step 2：运行红灯。** `cd apps/agent && uv run python -m pytest tests/unit/test_eval_preflight.py -q`；预期目标预检缺失导致失败，环境错误先修环境。
- [ ] **Step 3：实现预检和部署文档。** 通过 `load_settings` 和 MCP 的配置模型验证；检查启用图、真实模型版本、连接授权、知识 grant 与 source scope、端口及环回 HTTP 例外。固定本机端口 Agent=8000、MCP=8001；MCP `server.local_development=true`，Agent MCP 连接 `allow_loopback_http=true`，知识源 HTTP 只指向数字环回。绝不自动把 `scope_enforced` 改为 true。

Agent 侧必须写清的真实配置键：

```yaml
knowledge:
  enabled: true
  public_base_url: http://127.0.0.1:8000
  grants:
    - name: mcp-kb-reader
      team_id: 11111111-1111-1111-1111-111111111111
      business_team: ExampleTeam
      source_id: demo-knowledge
      token_secret_ref: env://WORKBENCH_KNOWLEDGE_TOKEN
      allowed_scopes:
        - service: checkout
          environment: test
          region: id
```

此段仅解释现有配置形状；实际 team_id/范围从用户创建的本机团队和 Agent 获取，不能把示例 UUID 当已存在实体。`WORKBENCH_KNOWLEDGE_TOKEN` 是 MCP→内部 KB；`WORKBENCH_MCP_AGENT_TOKEN` 是 Agent→MCP；`WORKBENCH_MCP_CURSOR_KEY` 是 MCP 游标密钥；模型 token 使用团队模型库已有环境引用。三类认证不可混用。

在独立 worktree 选择新的 `artifacts/eval-local/storage` 作为数据目录，检查目标不存在后才用 install/init-db；已有本地库使用显式配置选择新路径，不覆盖现有数据或改其存储选择。文档分别给出已有库与全新库分支：

```bash
./scripts/install.sh --data-dir "$PWD/artifacts/eval-local/storage"
# 编辑生成的本机配置；此命令仅适用于新选择的数据目录。
./scripts/init-db.sh
./scripts/start.sh --mcp
```

环境准备验收前先单独跑 `tests/contract/test_kb_internal.py`、`tests/integration/test_kb_mcp_chain.py` 与MCP的 `tests/contract/test_knowledge_gateway.py`。验证本机同scope可读、错误service/region/source/team/token不可读后，再由部署者将对应source的scope_enforced=true。索引检查命令从仓库根运行 `apps/agent/.venv/bin/workbench kb check-index --config config/local/agent.yaml --team-id "$EVAL_TEAM_ID"`，EVAL_TEAM_ID由会话配置的真实团队ID导出。

- [ ] **Step 4：绿灯和交付。** 运行 A1 单测及上述三组定向测试。交付可复制的双端配置对应表、四进程状态检查、模型连接检查和单条真实冒烟步骤；当前没有凭据时只验离线配置逻辑，真实冒烟标未执行。
- [ ] **Step 5：提交。** 明确添加本任务文件，建议 `docs: add local knowledge baseline setup and preflight`。

## Task A2：资料采集、导入及 source_id 对照（2–3 h）

**Files:** Create `evaluation/knowledge/collection-guide.md`、`seed-manifest.example.json`、`dataset.example.json`、`scripts/evaluation/import_seed.py`、`apps/agent/tests/unit/test_eval_import.py`；Create/Modify `evaluation/contracts.py`、`evaluation/api_client.py`。

**Interfaces:** `validate_seed_manifest(value: dict) -> list[dict]`；`async import_seed(api, manifest: dict, *, namespace: str, state_path) -> dict` 输出 `seed_id → entry_id/version/content_hash/chunk source_ids`。发布后读取精确版本，生成真实 `kb:entry@version#chunk`，不在标签里猜 UUID。

- [ ] **Step 1：先写测试。** 同一 namespace+seed_id+内容重复导入不新增条目；内容改变产生显式冲突而不静默发布；正文超过 16 KiB、未知 kind、通配范围、缺来源说明均失败。测试重复调用 importer 时 API 创建/发布次数各一次。
- [ ] **Step 2：红灯。** `cd apps/agent && uv run python -m pytest tests/unit/test_eval_import.py -q`。
- [ ] **Step 3：实现空模板、校验器及 API 导入。** 采用完整知识草稿 JSON：`seed_id, provenance, draft{title,kind,body_md,service,environment,region,tags,aliases,review_by}`。`provenance` 只进导入清单，不混入现有 KBDraft；正文通过 KBDraft 校验。导入顺序：创建草稿→发布→读版本 chunks→落导入映射；每次 POST 前持久化幂等键，恢复使用原键，If-Match 使用真实 revision。
- [ ] **Step 4：交付可直接填写的采集清单。** 内容要求如下，空清单本身不能通过业务评估：

| 交接项 | 数量/字段 | 责任 |
| --- | --- | --- |
| 真实知识 | 20–30 条，FAQ/Runbook/术语/案例，200–800 字建议；正文硬上限 16 KiB | 用户提供出处，Agent 整理格式 |
| 范围 | 至少 3–5 条通用、3–5 条其他业务范围；不可答标签要解释期望行为 | 用户确认 |
| 难例 | 2–3 对真实矛盾/历史版本、2–3 条过期资料、同义词/别名 | 只导入隔离评测团队，不污染日常知识 |
| dev 评测 | 30 条：12 answerable、5 multiple_sources、6 no_answer、3 conflict、2 stale、2 unauthorized | 用户标预期结论和来源 |
| holdout 评测 | 另收 ≥12 条，六场景均覆盖；独立受限文件，不给调优者查看 | 用户保管 |
| 标签 | id/question/query_scope/scenario/answerable/expected_source_ids/must_include/review_status/reviewer | source_id 导入后经映射确认 |
| 真实输出评审 | 每条的 reviewer/output_present/output_ref/claims/answer_usable/unauthorized_access/degradation_reason | 运行后用户逐条填写语义判断 |

`conflict/stale`是否可答由业务逐条标注，不全局假定。当前检索updated_at来自知识版本发布时间，新导入旧文档不会保留原发布年代；review_by也不能自动当成现有运行时freshness证据。过期案例需记录真实正文/来源时间和期望处理，暴露的差距如实进入基线，不篡改数据库时间让测试通过。30条核心业务集中的2条unauthorized使用可受理的调用范围，验证其他范围资料不被泄露，可获得真实Run并人工评审；额外再采2条受理即拒绝的协议用例，不占30条业务验收配额。协议用例保留HTTP证据，不能伪造Run；A4单独报告，B统一规则评分。

- [ ] **Step 5：绿灯和提交。** 运行 importer 单测及 `tests/integration/test_kb_entries.py`；仅提交模板和脚本，不提交用户原始私有资料。建议 `feat: add knowledge baseline collection kit`。

## Task A3：真实结果采集器与人工评审包（4–6 h）

**Files:** Create/补全 `evaluation/contracts.py`、`api_client.py`、`scripts/evaluation/collect_baseline.py`、`prepare_review.py`、`apps/agent/tests/unit/test_eval_contracts.py`、`test_eval_collection.py`。Read现有 `api/events.py`、`application/runs.py`（位于apps/agent/src/workbench_agent）；既有 `test_knowledge_evaluation.py` 只在发现真实兼容缺陷时修改。

**Interfaces:** 输入为 A2 的已标注 dev 数据集；输出 `experiment.json`、`journal.jsonl`、`results.jsonl`、`review.json`，目录为 `artifacts/evaluation/<experiment_id>/`。每个结果行固定：

```python
row = {
    'schema_version': 'eval-result-v1',
    'experiment_id': 'local-20261120-001',
    'case_id': 'case-001', 'repeat': 0,
    'request_key': 'persisted-before-send',
    'run_id': None,
    'transport_status': 'not_started',
    'run_status': None, 'outcome': None,
    'evidence': [], 'usage': None,
    'latency_ms': None, 'error_code': None,
}
```

以上为未执行记录示例；只有实际采集到的数据才能更新状态。experiment manifest 含 dataset/config/seed 哈希、git commit、graph_id、Profile VersionRef、模型 VersionRef、知识版本清单与哈希、采集器版本、开始时间、预算。配置 hash 排除 token 和机器绝对路径，包含影响行为的预算与授权配置。无法取得必需版本证据时禁止标为可比较。

- [ ] **Step 1：写身份和采集协议测试。** 直接使用待实现纯函数验证键：

```python
from workbench_agent.evaluation.contracts import make_run_key

def test_run_key_separates_experiments_and_reuses_resume():
    first = make_run_key('e1', 'dataset', 'config', 'case-1', 0)
    assert first == make_run_key('e1', 'dataset', 'config', 'case-1', 0)
    assert first != make_run_key('e2', 'dataset', 'config', 'case-1', 0)
    assert first != make_run_key('e1', 'dataset', 'config', 'case-1', 1)
```

另用 httpx.MockTransport 测 queued→running→partial，GET usage 的实际路径，逐 evidence_ref 获取证据；CreateRun scope 必须在 `business_context.query_scope`；每个 case 创建独立 Thread，不能相互污染历史。

- [ ] **Step 2：红灯。** `cd apps/agent && uv run python -m pytest tests/unit/test_eval_contracts.py tests/unit/test_eval_collection.py -q`。
- [ ] **Step 3：实现最小收集器与Run版本证据。** 创建请求前fsync journal，同时固定Idempotency-Key和client_message_id；POST网络超时保持原key再确认受理，不换key；保存accepted run_id后只轮询。无run_id的模糊受理超过服务器去重保留窗时停为needs_reconciliation，不自动重POST；当前幂等回执72h，client_message_id源事件90天，恢复前按实际版本核对。`EvaluationAPI.provenance`读取已有 `GET /v1/runs/{run_id}/events?after_seq=0` SSE，到run.accepted就封存payload.versions并主动关闭stream；其内容已有graph_id/agent_profile/default_model/role_models/tools及hash，不新增接口。遇410且本地没有已封存版本时标版本证据缺失，不能用Agent当前Profile补造。预期模型/Profile不符立即终止实验，不能混合版本输出。遇waiting_input/approval不自动编答案或同意，超时记录interrupted；终态含succeeded/partial/failed/cancelled/timed_out/expired。拒绝受理记transport_status=rejected和HTTP错误，不当作运行成功。有限重试处理429/短时5xx，遵守deadline；401/403不无限重试。A的串行采集先按配置模型调用/token最坏价格预留单案额度，不足即停止、不把未知用量当0；B4再将该检查扩为可恢复预算台账。
- [ ] **Step 4：实现评审预填。** 原始记录不变，单独生成 review 文件。仅预填 output_ref、输出存在性、引用列表、是否有引用这些机械字段；`citation_correct/answer_usable/unauthorized_access` 留 `null`，`review_status=pending`、reviewer 为空。缺少人工判断时现有 evaluate 必须报告 unavailable，不能默认 True/False 让其通过。每条 claim 的 `citation_correct` 由人工确认；未引用 claim 的该字段遵守现有 `None` 语义。
- [ ] **Step 5：绿灯和提交。** 验证 POST 响应丢失恢复不会多建 Run；重复 prepare_review 不覆盖已经填写的人工字段；结果/评审通过输出 hash 绑定，换结果则旧评审失效。建议 `feat: collect traceable knowledge baseline runs`。

## Task A4：真实运行、完整评审、基线与定向修复（3–5 h + 6–12 h 预留）

**Files:** Create 本地产物 `artifacts/evaluation/<experiment_id>/baseline.md`、`review.json`、`quality-report.json`；Modify 仅失败归因指向的实现/知识资料和对应测试；不是无条件重构。

**Interfaces:** Consumes A1 ready、用户真实模型与已审标签、A2 导入映射、A3 collector；Produces 可复核的 30+ dev 基线与逐案失败清单。没有真实输入时，A1–A3 可验收，A4 保持未完成。

- [ ] **Step 1：检查运行前条件。** 真实业务 dataset、30+ dev 标签完整、数据来源可核对；模型连接测试通过；知识索引一致；价格/调用上限/令牌上限经用户明确提供。首次只跑 1 条并核对链路和计费，再扩至 30 条。未获真实运行输入前不发送模型请求。
- [ ] **Step 2：运行 collector 并封存。** 保存每案 Run 与证据，不修改原输出；内部源版本发生变化时终止本实验并新建实验，不能混一批知识版本计算单一分数。费用未知不能作为 0；原始内容留本机受限产物目录。
- [ ] **Step 3：用户完整评审。** 正反例、所有输出 claim 和引用逐条审核；对结果的抽样复核可额外进行，不能代替 30 条完整标注。授权拒绝且无 Run 的用例单独列协议结果，不能为了满足旧汇总器伪造 output_present。
- [ ] **Step 4：汇总并保留失败退出码。** 仓库根目录执行：

```bash
apps/agent/.venv/bin/workbench evaluate --dataset-type knowledge --dataset artifacts/evaluation/local-baseline/review.json --output artifacts/evaluation/local-baseline/quality-report.json
```

`local-baseline` 是本次实际选择的 experiment_id；报告 failed/unavailable 会退出非零，仍保存报告。报告明确：完整可评案数/全部案数、引用分母、可答分母、不可答结论总数及涉及用例数、越权数、拒绝受理数、缺评数、场景分布、模型/知识指纹和费用待确认数。旧汇总器不能表达的协议案例另列；B 才统一报表，不改变 A 原始证据。

- [ ] **Step 5：按真实失败分类修复。** 召回缺陷→别名/切块测试；综合答案缺陷→提示/校验测试；资料缺口→补真实条目；授权缺陷→scope 契约测试。每个修复先写可复现失败测试、最小修复、定向绿灯。每轮新 experiment_id、同一 dev 标签重跑；保留修复前报告。最多先安排 1–2 轮，超出预留后列差距和新增估算。
- [ ] **Step 6：交付。** baseline.md 给出达标与否、逐案原因分类、修复收益和余留限制；首次未达标是有效测量，不宣称质量已验收。脱敏汇总可单独提交 `docs: record first real knowledge baseline`，私有内容和密钥不提交。

## 本包验收

- [ ] 未准备资料时，已经有用户可填写的知识模板、标注模板和采集清单，而非只有口头依赖。
- [ ] scope 授权和三个认证方向有对应表；同机端口冲突已处理；未覆盖已有库。
- [ ] 运行与引用可追溯至真实版本；review 与输出绑定；语义判定无人代填。
- [ ] 真实报告包含失败、不完整和协议拒绝，未把缺失或 synthetic 算作通过。

# 真实知识问答基线采集清单

这个目录用于收集**真实业务材料**。示例文件只定义格式，不是业务数据，也不构成成功基线。原始材料可以含内部信息，必须保存在本机受限目录，不要提交到 Git。

## 1 用户需要准备的资料

| 项目 | 数量与边界 | 验收规则 |
| --- | --- | --- |
| 知识条目 | 20–30 条，FAQ、Runbook、术语或案例，建议 200–800 字；硬上限 16 KiB | 每条有合法来源、负责人、适用范围和复核日期 |
| 通用范围 | 3–5 条 | 可被多个业务范围共同使用，但不能越出授权范围 |
| 其他范围 | 3–5 条 | 与主范围不同，用于验证 scope 隔离 |
| 冲突/版本 | 2–3 对 | 记录各版本的正文时间与真实期望处理 |
| 过期资料 | 2–3 条 | 保留原始来源时间和内容；不修改数据库时间来制造过期 |
| 同义词 | 每组 2–4 个真实叫法 | 写进知识草稿的 `aliases`，并配至少一个问题 |
| dev 问题 | 至少 30 条 | 12 answerable、5 multiple_sources、6 no_answer、3 conflict、3 stale、2 unauthorized 为建议配额；场景可逐条调整，但六种场景都必须出现 |
| holdout | 另收至少 12 条 | 六种场景均覆盖，独立保管，不用于调优 |
| 协议拒绝用例 | 另收至少 2 条 | 预期在受理阶段拒绝；保留 HTTP 状态与错误码，不伪造 Run |

“unauthorized”的语义需要逐条确认。若系统可以在受理后运行、但必须不泄露其他范围内容，标记为 `unauthorized` 并计入 30 条业务集；若预期在受理阶段直接拒绝，则放入单独的协议拒绝清单，不能伪装成有 Outcome 的业务案例。

冲突和过期的案例必须明确标注 `answerable`。不要把所有 `conflict` 或 `stale` 默认当作不可答。

## 2 知识模板字段

复制 `seed-manifest.example.json`，每条填写：

| 字段 | 要求 |
| --- | --- |
| `seed_id` | 在 `seed_namespace` 内稳定且唯一；同一 ID 内容不变可重跑，内容改变会明确冲突 |
| `provenance.source` | 原文 URL、文档系统位置、标题、负责人和原始日期 |
| `draft.title` | 可在页面显示的标题，最多 200 字符 |
| `draft.kind` | 仅 `faq`、`runbook`、`glossary`、`case` |
| `draft.body_md` | 已复核 Markdown，UTF-8 不超过 16 KiB |
| `draft.service` | 真实服务；可空，不能写 `all`、`*` 或逗号列表 |
| `draft.environment` / `draft.region` | 真实范围；可空，不支持通配符 |
| `draft.tags` / `draft.aliases` | 最多 16 个名称，每个不超过 64 字符 |
| `draft.review_by` | 业务复核期限；它不是运行时 freshness 证据 |

导入只把 `draft` 写入知识管理 API；`provenance` 留在本地清单。导入成功后按实际发布版本和 chunk 生成 `kb:entry@version#chunk`，不在问题标签里猜 UUID。

## 3 问题与标签

复制 `dev-questions.example.json`，每条填写：

| 字段 | 要求 |
| --- | --- |
| `id` | 数据集内稳定唯一 |
| `question` | 真实问题，与已有问题及 scope 不重复 |
| `query_scope` | `service/environment/region/start_at/end_at`，窗口大于 0 且不超过 1 小时 |
| `scenario` | `answerable`、`multiple_sources`、`no_answer`、`conflict`、`stale`、`unauthorized` 之一 |
| `answerable` | 布尔值；与预期行为一致 |
| `expected_source_ids` | 导入后确认的精确 chunk ID；可答题至少一个，多来源题至少两个 |
| `must_include` | 输出必须包含的业务事实/条件 |
| `review_status` | `pending` 或 `reviewed` |
| `reviewer` | 实际审核人，不能写模型名 |
| `notes` | 场景为什么可答/不可答、已知歧义与降级期望 |

问题提交后，每个用例使用独立 Thread。每次采集都固定 experiment、dataset/config 哈希、case 和 repeat，形成独立幂等键。

## 4 运行后的完整人工评审

`prepare_review.py` 只预填机械字段：Run 是否存在、`output_ref` 和输出中的引用。它把语义判断保持为 `null`。

每条输出都要人工填写：

| 字段 | 人工判断 |
| --- | --- |
| `reviewer` | 审核人 |
| `output_present` | 实际是否存在可评输出；拒绝受理通常为 `false` |
| `output_ref` | Run ID 或协议证据引用 |
| `claims[].citation_correct` | 引用是否真实支持该 claim；被引用 claim 必须明确 `true/false` |
| `claims[].cited` | 没有引用时写 `false`，并把 `citation_correct` 保持 `null` |
| `answer_usable` | 业务人员是否可用 |
| `unauthorized_access` | 是否泄露未授权资料或访问越权 |
| `degradation_reason` | 不可答/失败的真相及可用降级 |
| `review_status` | 只有完整审核后才是 `reviewed` |

只抽样不能满足 `knowledge_report.py` 的 `missing_reviews=0`。无法判定时必须保留缺失或待复核，不能按正确/可用默认。

## 5 交接前的静态检查

在首次真实模型运行之前完成：

- 每条知识都有来源、范围、负责人和复核日期。
- 知识清单可通过 `validate_seed_manifest`。
- dev 至少 30 条，单独 holdout 至少 12 条；问题与 scope 无重复。
- 六种场景都有真实样本，2 个以上可答问题确实需要多个来源。
- 每条 `expected_source_ids` 对应已发布版本的精确 chunk，而不是标题或模糊查询。
- 冲突/过期资料的可答性已由业务确认，原始时间保留。
- 所有预期越权用例已分成“可运行但不泄露”和“受理即拒绝”两类。
- 已经核对模型、Profile、图、知识索引、授权和费用预算。

完成交接后再运行：

```bash
export WORKBENCH_BASELINE_TOKEN='<runtime-injected-token>'
./scripts/evaluation/preflight.py --config config/evaluation/local.yaml
./scripts/evaluation/import_seed.py --config config/evaluation/local.yaml
./scripts/evaluation/collect_baseline.py --config config/evaluation/local.yaml \
  --experiment-id local-20261120-001
./scripts/evaluation/prepare_review.py --config config/evaluation/local.yaml \
  --experiment-id local-20261120-001
```

完整评审后执行现有 gate：

```bash
apps/agent/.venv/bin/workbench evaluate \
  --dataset-type knowledge \
  --dataset artifacts/evaluation/local-20261120-001/review.json \
  --output artifacts/evaluation/local-20261120-001/quality-report.json
```

拒绝受理的协议用例应单独保存证据并在报告中列出。状态为 `needs_reconciliation` 的命令退出码为 2；此时查证原幂等键对应的 Run，不要创建新 key 重发。

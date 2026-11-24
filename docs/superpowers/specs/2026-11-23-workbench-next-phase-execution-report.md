# Workbench 下一阶段执行记录

> 日期：2026-11-23，Asia/Shanghai。基线：`a3396e9`。
> 本文只记录本轮已取得的证据。未运行的环境验收、真实上游和外部依赖
> 不得据离线测试推断为完成。

## 已实现

| 范围 | 实现 | 证据 |
| --- | --- | --- |
| P0 | 文档门禁不再按 800 行产生机械告警；修正失效提交引用、页面状态、当前 schema 与 D3 交付口径 | `check_docs.py` 为 9 files、0 errors、0 warnings；文档门禁测试 7/7 通过 |
| Q1 | 新增共享 request bound 与 summary policy；新 `knowledge.citation_review`、`conversation.tool_use` Run 冻结 16 KiB、8 消息和摘要模型输出预留；其他图保留旧策略 | 纯函数与预算/图回归通过 |
| Q2 | 截断诊断从持久化工具回执恢复；JSON 修复只允许一次并记录事件；范围词不再独立驱动内容检索 | `test_knowledge_failure_recovery.py` 及 Q2 定向集 77/77 |
| Q3 | `engineering` 只接受 synthetic/dev；合成报告强制 `quality_status=unavailable`、`release_passed=false`；业务默认不变 | 边界测试与评测离线集 67/67 |
| R1 | 从已有 Run/Interrupt/Checkpoint、Action/Approval/Outbox 快照生成同一资格判定；connector、actor、审批人、撤权与过期仍不可操作 | Run 输入、资格表、现有动作审批和恢复契约回归通过 |
| R2 | 新增 `GET /v1/reviews`、`/summary`、`/{item_id}`；权限候选先于分页，游标绑定 team/subject/role/team revision/filter；新增 `0007_review_indexes` | 审核 API 7/7；迁移与升级测试 48/48；10,000 来源行容量测试通过 |
| R3 | 新增幂等 `POST /v1/actions/{id}/reconcile`；只调用发送器只读 lookup | 对账成功、重放、unknown、撤权、无 lookup 6/6 |
| R4 离线 | 审核中心路由/列表/徽标/动作卡片/纯文本网页来源组件 | TypeScript 检查通过；Vitest/Vite 被本机 Rollup 签名错误阻塞 |
| E1 | 新工具对话 runner 复用 request key、journal、恢复和现有评分器 | 工具 runner + 既有评测测试 31/31 |
| W1 | `web_search` 声明式映射、模型最小 schema、受保护字段和示例指纹 | 映射及已有映射集 26/26 |
| W2 离线 | 网页 envelope 状态校验、URL/域名限制、来源展示和无全文声明 | W1/W2 Python 集 26/26；Web 类型检查通过 |

## 验证

```bash
python3 scripts/check_docs.py
python3 -m unittest scripts.tests.test_check_docs -v
git diff --check
PYTHONPATH=<worktree>/apps/agent/src \
  <main-workspace>/apps/agent/.venv/bin/python -m pytest \
  tests/unit/test_summary_policy.py \
  tests/unit/test_review_eligibility.py \
  tests/integration/test_expert_budget.py \
  tests/integration/test_summary_graph.py \
  tests/integration/test_public_knowledge_qa.py \
  tests/integration/test_tool_conversation_graph.py \
  tests/integration/test_action_contract.py \
  tests/integration/test_resume_protocol.py \
  tests/integration/test_reviews_api.py \
  -q -p no:cacheprovider
```

最新代码验证：

- Agent 定向集：`215 passed in 77.79s`。
- Agent 全量：`1560 passed, 18 skipped, 4 failed, 8 errors`；12 个
  localhost/子环境用例在受控沙箱外重跑为 `12 passed in 58.71s`。
- Scripts：`28 tests OK`。
- Web：44 个文件、166 项通过；TypeScript 与 Vite production build 通过。

## 环境限制

- 隔离 worktree 的 `.venv` 与 `__pycache__` 目录不在当前沙箱写权限内。
  测试使用主工作区已安装依赖的解释器，并以 `PYTHONPATH` 指向本 worktree；
  Python 测试和 OpenAPI 导入均通过。
- 分支引用和 Git index 位于主仓 `.git`，本轮沙箱不允许创建分支或提交。
  修改仅保留在隔离 worktree，未提交、未推送。
- 默认桌面 Node 与 Rollup 原生模块存在 Team ID 签名冲突；改用系统
  `/usr/local/bin/node` 后 Web 测试与构建全部通过。
- 隔离 worktree 默认 `uv` Python 为 x86_64 且 SQLite 版本较低；验证使用
  Homebrew Python 3.12 / SQLite 3.53.1。该环境差异不能作为产品降级依据。

## 尚未完成

- Q3 的首轮成功数聚合仍需扩充。
- E2–E3 真实工具采集、A4 真实基线、最终报告合并和 nightly 启用。
- W3 `web_search` 真实上游、双工具隔离和人工验收。
- 真实模型、真实知识源、发布 `knowledge_search`、`web_search` 上游与
  人工业务标签。以上依赖未提供，不能以离线夹具替代。

## 复核要点

- 新 Run 的摘要预留公式已经冻结；默认上限的完整 graph 级裁剪仍应在真实
  检索规模下验证。
- R2 已新增索引迁移并验证 10,000 来源行，但仍应在目标部署记录真实查询计划。
- 未提交或推送，因为当前 worktree 的 Git refs/index 写入仍受权限限制。

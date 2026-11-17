---
title: DoD 源码迁移：迁移前测试基线
date: 2026-11-14
---

迁移前 HEAD：`f7271b9252195a13f6d0774de099214b3c6016f6`，分支 `master`。Agent 和 MCP Server 合计 545 个已跟踪文件。工作区原有 career 文档修改与未跟踪资源，以及 `projects/dod-agent/capture.pcap`，均不属于迁移内容。

环境：Python 3.12.3，SQLite 3.53.1，Node.js 22.23.1，npm 10.9.8。当前命令路径未发现 Docker。已用旧版 checkpoint codec 生成不含用户数据的合成样本供后续兼容检查。

- Agent：`PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider -q`，1047 passed、17 skipped、1 failed；耗时 624.77 秒。失败是 `tests/contract/test_delivery_packaging.py::test_tracked_source_wheel_includes_delivery_without_untracked_init` 假定 `delivery/__init__.py` 未被跟踪；实际 HEAD 中该文件已被跟踪，历史提交 `b29af2b` 将其加入。迁移需保留 Git archive 和 wheel 内容验证，修正过时假设。
- MCP Server：相同 pytest 调用 72 passed；`ruff check --no-cache .` 和 `ruff format --check --no-cache .` 均通过。
- Web：`npm test` 10 passed；`npm run typecheck` 和 `npm run build` 均通过。基线使用已有 node_modules；未运行 npm ci。
- Agent 中 15 项 live 测试默认跳过；另外 2 项跳过需在最终完整回归中与基线逐项对照。

测试使用现有虚拟环境和依赖，未重新执行 uv sync。新增的目标结构 wheel 测试在旧版上按预期失败：wheel 尚不包含 `workbench_agent/api/frontend.py`、`workbench_agent/delivery/dispatcher.py` 和 `workbench_agent/persistence/migrations/env.py`。

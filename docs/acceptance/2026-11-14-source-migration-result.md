---
title: DoD 源码迁移：验证记录与待验收事项
date: 2026-11-14
---

## 迁移结果

Agent、Web 和 MCP Server 的 Git 跟踪源码已迁入 `projects/dod/apps/`；Agent Python 入口使用 `workbench_agent` 命名空间，原有 CLI 名称保持。MCP 使用 `workbench_mcp_server`，前端独立构建，API 从可配置的前端目录提供 `/app/`。原始规范与文档移至 `docs/`，部署文件移至 `deploy/`。

迁移前 HEAD 为 `f7271b9252195a13f6d0774de099214b3c6016f6`。迁移前基线见 [基线记录](2026-11-14-source-migration-baseline.md)。迁移在本地 `master` 分阶段提交；远程未推送。

迁移时保留的旧目录及其本地文件已于 2026-11-15 移入废纸篓；迁移验收期间未对旧运行数据进行修改。工作区原有 career 文档改动未纳入迁移提交。

## 已执行的验证

| 项目 | 实际结果 |
| --- | --- |
| Agent 全量离线 pytest | 1052 passed，17 skipped，619.16 秒；跳过项全部要求 `--run-live`，包含两个 QA live 项目 |
| MCP Server pytest | 72 passed；Ruff check 和 format --check 通过 |
| Web | npm ci 安装成功；10 项测试、vue-tsc 类型检查、Vite 生产构建通过 |
| Agent/MCP 重点链路 | 包含 MCP 映射、多图、知识、摘要、SOP、M3 合成旅程和页面检查，共 87 passed |
| Agent 发行包 | 从 Git 快照构建 wheel；在源码目录外独立安装，验证 CLI、数据库迁移、Graph 注册、API 静态页面和 evaluate；安装测试 5 passed |
| MCP 发行包 | wheel 构建成功；源码目录外的独立虚拟环境安装并执行 CLI `--help` 成功 |
| 启动演练 | 用独立临时 SQLite 数据库执行 migrate、bootstrap、doctor、worker --once、scheduler --once，API ready；`/app/` 与深链接返回 200 |
| 旧 checkpoint 样本 | 迁移前编码的合成 JSON checkpoint 由迁移后 codec 成功解码；既有恢复和 checkpoint 测试包含在 Agent 全量回归中 |
| 文档与部署静态检查 | 项目入口/组件/spec 的相对链接可解析；Compose YAML、构建输入、MCP 容器示例监听地址通过静态检查 |
| 独立代码复核 | 修复 MCP 容器示例的监听/Host 配置；将开发模式 API 的宿主机端口限定为 127.0.0.1 |

迁移前 Agent 基线为 1047 passed、17 skipped、1 failed。唯一失败的 Git archive 测试对 `delivery/__init__.py` 是否被跟踪的假设已经过时；迁移后修正该假设，保留 wheel 内容验证。

## 尚未完成的验收

- 本机未发现 Docker 命令，因此 **Docker 镜像构建、Compose 实际运行、容器网络和 TLS 代理尚未验证**。在安装 Docker 的环境按 [启动手册](../runbooks/source-layout.md) 执行构建和隔离测试卷演练。
- 真实企业知识、监控、日志、Trace 等数据源未在本次迁移中接入或验收；17 个 live 测试未执行。
- checkpoint 的跨版本检查使用合成 codec 样本，并结合迁移后的恢复测试；未对用户现有数据库执行迁移或恢复演练。
- Web 仍以示例数据为主；目录迁移不等同于 M4 工作台功能和业务联调完成。

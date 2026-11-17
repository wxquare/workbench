---
title: 我的工作台：文档导航
---

# 我的工作台（Workbench）：文档导航

三份文档面向不同读者，按"一类读者、一个问题"拆分自原技术规格书（`WORKBENCH-PY-SPEC-001 v1.9`，4324 行）。

## 文档地图

| 文档 | 读者 | 回答 | 篇幅 |
| --- | --- | --- | --- |
| [01 产品文档](01-product.md) | 产品、业务方、值班团队、管理员 | 做什么 / 为谁 / 做到哪 / 怎么验收 | 800 行 |
| [02 技术设计文档](02-technical-design.md) | 架构师、评审人、核心开发 | 为什么这样设计 / 契约与边界 / 取舍 | 761 行 |
| [03 实现细节文档](03-implementation.md) | 开发、测试、运维 | 代码在哪 / 怎么跑 / 怎么改 / 数据怎么存 | 880 行 |

## 实现细节附录

| 附录 | 内容 | 篇幅 |
| --- | --- | --- |
| [附录 A 数据字典](03-implementation/A-data-dictionary.md) | 61 张表、586 个字段的完整字段字典与演进历史 | 2094 行 |
| [附录 B 接口参考](03-implementation/B-api-reference.md) | 65 个 HTTP 入口的请求/响应与统一约定 | 408 行 |
| [附录 C 配置参考](03-implementation/C-config-reference.md) | Agent 与 MCP Server 的逐项配置参考 | 427 行 |

## 按角色的阅读路径

### 产品 / 业务方

1. [01 产品文档](01-product.md)：产品定位 → 用户与场景 → 能力清单 → 路线图
2. 需要了解术语时查阅第 10 节术语表

### 架构师 / 评审人

1. [02 技术设计文档](02-technical-design.md)：设计目标 → 架构决策 → 领域模型 → 状态机 → 风险
2. 需要核对存储时查阅 [附录 A 数据字典](03-implementation/A-data-dictionary.md)

### 新加入的核心开发

1. [01 产品文档](01-product.md) 第 1–4 节：了解产品定位和能力边界
2. [02 技术设计文档](02-technical-design.md)：理解架构决策和领域模型
3. [03 实现细节文档](03-implementation.md)：代码地图 → 一次请求的代码路径 → 开发指引

### 日常开发 / 排障

1. [03 实现细节文档](03-implementation.md) 第 1–3 节：代码地图和请求路径
2. [附录 B 接口参考](03-implementation/B-api-reference.md)：接口契约
3. [附录 C 配置参考](03-implementation/C-config-reference.md)：配置项查找
4. [附录 A 数据字典](03-implementation/A-data-dictionary.md)：表结构查询

## 文档状态

- **版本**：基于 `WORKBENCH-PY-SPEC-001 v1.9` 拆分，经源码核对更新。
- **图表**：复用 `../../assets/workbench-python/` 下 19 张图。部分 ER 图标注为"重构前快照"（使用旧 Workspace 命名），各附录中的 Mermaid erDiagram 为按 schema_agent_v3 重新生成的当前关系。
- **术语**：全文统一使用 Agent / Thread / Run / Job / Skill；Workspace 仅作为"已移除概念/容器目录"出现在迁移附录。
- **能力状态**：所有能力用"已实现 / 部分 / 规划"三态标注，依据源码实际存在性判定。

## 相关文档（仓库内）

- 运维操作：[运维手册](../../runbooks/) 下的启动手册、SQLite 恢复、Skill 导入等
- 验收记录：[验收记录](../../acceptance/) 下的各阶段验收报告
- MCP Server：[MCP Server 文档](../../mcp-server/) 下的 MCP Server 设计与验收文档
- 原始规格书：[原始规格书](../2026-11-12-workbench-python-spec.md)

- 差异清单：[原文与源码差异](DIFF-REGISTER.md)

## 图表索引

| 编号 | 内容 | 文件 |
| --- | --- | --- |
| 01 | 整体架构 | `../../assets/workbench-python/01-architecture.png` |
| 02 | 启动流程 | `../../assets/workbench-python/02-startup.png` |
| 03 | 工作流目标架构 | `../../assets/workbench-python/03-workflow.png` |
| 04 | 任务运行与交付时序 | `../../assets/workbench-python/04-runtime.png` |
| 05 | 输入与确认恢复时序 | `../../assets/workbench-python/05-interrupt-resume.png` |
| 06 | 故障恢复与对账时序 | `../../assets/workbench-python/06-recovery.png` |
| 07 | Run 状态机 | `../../assets/workbench-python/07-run-state.png` |
| 08 | 存储架构 | `../../assets/workbench-python/08-storage.png` |
| 09 | 外部动作状态机 | `../../assets/workbench-python/09-action-state.png` |
| 10 | 存储接口与后端 | `../../assets/workbench-python/10-storage-backends.png` |
| 11 | 领域关系总览 | `../../assets/workbench-python/11-domain-overview.png` |
| 12 | 配置与版本固定 ER | `../../assets/workbench-python/12-config-version-er.png` |
| 13 | 执行、等待与恢复 ER | `../../assets/workbench-python/13-run-recovery-er.png` |
| 14 | 范围、证据与结果 ER | `../../assets/workbench-python/14-evidence-outcome-er.png` |
| 15 | 调用、预算与结算 ER | `../../assets/workbench-python/15-tool-usage-er.png` |
| 16 | 审批、投递与事件 ER | `../../assets/workbench-python/16-action-delivery-er.png` |
| 17 | 巡检与工作流 ER | `../../assets/workbench-python/17-schedule-run-er.png` |
| 18 | 部署进程 | `../../assets/workbench-python/18-deployment-processes.png` |
| 19 | LangGraph 与 LangChain 关系 | `../../assets/workbench-python/19-langchain-langgraph.png` |

每种图同时提供 PNG 阅读版、SVG 矢量版和 Mermaid 图源（`.mmd`）。

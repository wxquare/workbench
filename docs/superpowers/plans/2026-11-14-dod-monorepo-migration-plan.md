---
title: DoD 源码迁移方案：统一项目目录与 Python 包边界
date: 2026-11-14
---

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将现有 `projects/dod-agent` 和 `projects/workbench-mcp-server` 整理为 `projects/dod`，使 Agent、Vue 工作台和 MCP Server 具有明确的源码、构建和运行边界，并保持既有业务行为与数据兼容。

**Architecture:** 采用单仓库、多应用结构；Agent API、Worker、Scheduler 继续来自同一个 Python 应用，MCP Server 独立运行，Web 独立构建并由 Agent API 提供静态文件。两个 Python 应用各自保留 pyproject.toml 和 uv.lock，前端保留 package-lock.json；本轮不引入共享 Python 包或统一 uv workspace。

**Tech Stack:** Python 3.12、setuptools/uv、FastAPI、LangGraph、SQLite/SQLAlchemy/Alembic、MCP、Vue 3、TypeScript、Vite、Element Plus、npm、Docker Compose。

**状态：** 源码迁移及本机回归已执行；Docker 镜像、容器网络和 TLS 代理仍待有 Docker 的环境验收。执行证据与限制见 [迁移验证记录](../../acceptance/2026-11-14-source-migration-result.md)。以下清单保留为原始执行计划。

## 全局约束

- Python 版本保持现有 `>=3.12,<3.13`；Agent SQLite 最低版本保持 `3.51.3`。
- 保留发行包名称 `workbench-agents`、`workbench-mcp-server`，保留 CLI `dod`、`workbench-mcp-server` 及现有辅助 CLI 名称。
- Agent 导入前缀统一为 `workbench_agent`；MCP Server 保留 `workbench_mcp_server`。
- `/v1` API、`/app/` 页面路径、SSE 协议、MCP 工具名、权限规则、Graph/Checkpoint 版本保持兼容。
- 本轮不修改数据库 schema、历史迁移 revision、业务 ID、工具调用协议、模型配置和真实数据源授权。
- 不把真实企业数据源验收、M4 占位页面补齐或其他功能开发纳入目录迁移。
- 本地运行数据、凭据、数据库、抓包和缓存不自动迁移进 Git；运行数据仍由绝对路径配置管理。
- 迁移源码、文档和部署配置时同步修正引用；每个阶段到达可验证状态后再提交。
- 当前仓库是 `/Users/xianguiwang/Projects/wxquare-private`；本方案中的执行路径均相对于该仓库根目录。代码块内另有说明的除外。
- 尊重用户已选择的当前工作区和 master；执行前记录工作区变化，避免将已有 career 文档和未跟踪抓包混入迁移提交。

## 当前发现与迁移策略

| 现状 | 迁移时的处理 |
|---|---|
| Agent 直接安装 `api`、`domain`、`tools`、`settings` 等顶层模块 | 收入 `src/workbench_agent/`；同步源码、测试、入口和动态导入 |
| `src/web/routes.py` 是 Python 静态路由，外层 `web/` 才是 Vue | Python 文件迁为 `workbench_agent/api/frontend.py`；Vue 迁为 `apps/web/` |
| setuptools 的包列表没有包含当前 `src/web` | 修正包发现规则，并在安装后的 wheel 中实际创建 API 与访问页面，不能只检查源码导入 |
| 若干集成测试使用 `parents[3] / 'workbench-mcp-server' / 'src'` | 使用新的 `apps/mcp-server/src`，并集中到共享 fixture；项目内组件缺失不能悄悄 skip |
| 打包测试写死 `git archive HEAD projects/dod-agent` 和仓库层级 | 动态定位 Git 根及应用相对路径；验证当前提交中的新目录 |
| 静态资源路由默认读取 `asset_root/web/dist` | 增加可配置前端构建目录，保留原有默认行为；本地和容器显式指定 |
| 现有 Dockerfile 只有 Python 构建，未构建/复制 Vue dist | 以 DoD 项目根为上下文，增加 Node 构建阶段并复制产物 |
| Compose 目前只定义 API、Worker、Scheduler | 修正三者构建和配置路径；MCP 使用独立 profile，示例源不直接当作可用企业源 |
| MCP README 仍有 Agent MCPAdapter 未实现的历史表述 | 更新为当前实现事实，明确本地协议验证与真实数据源验收的区别 |

## 目标目录与文件职责

```text
projects/dod/
├── README.md                         统一入口、组件说明、快速启动
├── .gitignore                        项目级缓存、产物与运行目录忽略规则
├── .dockerignore                     过滤凭据、运行数据、缓存、抓包和构建产物
├── docs/
│   ├── specs/                        原始 Markdown spec 与配套 HTML/ZIP
│   ├── assets/                       spec/正式文档静态资源
│   ├── acceptance/                   Agent 原有验收材料
│   ├── runbooks/                     原有运维手册与新目录启动说明
│   ├── superpowers/plans/            Agent 原有计划及本迁移方案
│   └── mcp-server/                   MCP 原有文档，保留内部目录避免重名覆盖
├── deploy/
│   ├── Dockerfile.agent              Python + Vue 构建，API/Worker/Scheduler 共用
│   ├── Dockerfile.mcp-server         独立 MCP 镜像
│   └── compose.local.yml             项目级服务编排
├── config/
│   ├── local/
│   │   ├── agent.yaml                原 Agent local.yaml
│   │   └── mcp-server.example.yaml   原 MCP example.yaml；需配置后启用
│   └── workflows/                    原工作流配置
└── apps/
    ├── agent/
    │   ├── pyproject.toml
    │   ├── uv.lock
    │   ├── .python-version
    │   ├── alembic.ini                原文件保留，并修正其中的旧路径
    │   ├── src/workbench_agent/
    │   │   ├── __init__.py
    │   │   ├── settings.py
    │   │   ├── api/frontend.py        SPA 静态路由
    │   │   ├── application/           原目录整体保留
    │   │   ├── domain/
    │   │   ├── runtime/
    │   │   ├── tools/
    │   │   ├── persistence/
    │   │   ├── cli/
    │   │   └── ...                    connectors/delivery/evaluation/ingress/
    │   │                              incidents/jobs/runner/skill_management/workspaces
    │   └── tests/                     保留 unit/contract/integration/e2e/fault 等分类
    ├── web/
    │   ├── README.md
    │   ├── package.json
    │   ├── package-lock.json
    │   ├── vite.config.ts             继续使用 base: /app/
    │   ├── src/                       原 Vue 源码整体搬迁
    │   └── tests/
    └── mcp-server/
        ├── README.md                  组件级协议与连接器说明，链接到项目文档
        ├── pyproject.toml
        ├── uv.lock
        ├── .python-version            若原目录存在则保留
        ├── src/workbench_mcp_server/         原内部结构保持
        └── tests/
```

`apps/web/src/api/` 和 `src/router/` 可在后续前端业务开发时按需建立；这次保留现有 `router.ts`、`sse.ts` 等文件布局，避免把页面重构混进迁移。未在上图逐项列出的已跟踪源码和正式资源，必须先盘点再归位，不得遗漏。

## 路径映射

| 当前路径 | 最终路径 |
|---|---|
| `projects/dod-agent/src/*` | `projects/dod/apps/agent/src/workbench_agent/*` |
| `projects/dod-agent/src/web/routes.py` | `projects/dod/apps/agent/src/workbench_agent/api/frontend.py` |
| `projects/dod-agent/web/*` | `projects/dod/apps/web/*` |
| `projects/dod-agent/tests/*` | `projects/dod/apps/agent/tests/*` |
| `projects/dod-agent/{pyproject.toml,uv.lock,.python-version,alembic.ini}` | `projects/dod/apps/agent/` 下同名文件 |
| `projects/dod-agent/config/local.yaml` | `projects/dod/config/local/agent.yaml` |
| `projects/dod-agent/config/workflows/*` | `projects/dod/config/workflows/*` |
| `projects/dod-agent/deploy/Dockerfile` | `projects/dod/deploy/Dockerfile.agent` |
| `projects/dod-agent/deploy/compose.local.yml` | `projects/dod/deploy/compose.local.yml` |
| `projects/dod-agent/docs/*` | `projects/dod/docs/*` |
| `projects/dod-agent/2026-11-12-workbench-python-spec.*` | `projects/dod/docs/specs/` 下同名文件 |
| `projects/dod-agent/assets/*` | `projects/dod/docs/assets/*`，修正 spec 内相对链接 |
| `projects/workbench-mcp-server/{src,tests,pyproject.toml,uv.lock,README.md}` | `projects/dod/apps/mcp-server/` 下同名项 |
| `projects/workbench-mcp-server/config/example.yaml` | `projects/dod/config/local/mcp-server.example.yaml` |
| `projects/workbench-mcp-server/docs/*` | `projects/dod/docs/mcp-server/*` |

本地 `.venv/`、`node_modules/`、`dist/`、`build/`、`*.egg-info/`、`.pytest_cache/` 不作为源码搬迁，应在新位置重新安装或构建。现有 `var/`、数据库与凭据留在原位置，记录绝对路径并按运行配置继续访问；若旧父目录因此仍存在，这是本地运行数据保留，不是源码迁移遗漏。禁止直接对整个旧目录执行递归删除。

## Task 1：建立基线与迁移清单

**涉及文件：** 创建 `projects/dod-agent/docs/acceptance/2026-11-14-source-migration-baseline.md`；迁移后进入 `projects/dod/docs/acceptance/`。

**输入/输出：** 输入当前 master 与工作区状态；输出基线提交 SHA、已跟踪文件清单、测试通过/失败/跳过清单、环境要求和旧运行路径记录。

- [ ] 记录 `git rev-parse HEAD`、`git status --short` 和 `git ls-files projects/dod-agent projects/workbench-mcp-server`；正式材料只记录路径，不记录凭据值。
- [ ] 确认 SQLite 运行时版本达到 3.51.3；不能用系统自带旧 SQLite 的失败替代业务基线。
- [ ] 在原 Agent 目录执行 `uv sync --frozen --group dev`、`uv run python -m pytest`。
- [ ] 在原 MCP 目录执行 `uv sync --frozen --group dev`、`uv run python -m pytest`、`uv run ruff check .`、`uv run ruff format --check .`。
- [ ] 在原 Web 目录执行 `npm ci`、`npm test`、`npm run typecheck`、`npm run build`。
- [ ] 核查所有 skip，单列需要凭据的 live 测试；涉及项目内 MCP 的离线链路必须实际运行。已有失败先归因记录，不能在迁移后以“原来就可能失败”解释。
- [ ] 提交基线文档：`docs: record DoD source migration baseline`；已有无关工作区修改继续保留。

**通过条件：** 能明确比较迁移前后测试差异；没有未解释的阻塞失败。此阶段不搬目录。

## Task 2：在旧应用位置先统一 Python 包名

**涉及文件：** `projects/dod-agent/src/`、`pyproject.toml`、`uv.lock`、`tests/`；`src/api/app.py`、`src/web/routes.py`、`src/cli/db.py`、`src/persistence/migrations/` 是重点核查对象。

**接口：** 输出可安装的 `workbench_agent` 包；CLI 名称、HTTP 与 MCP 契约保持原样。此任务完成时应用仍位于旧目录，便于单独验证包名改动。

- [ ] 先更新 `tests/contract/test_package_layout.py`：要求 wheel 包含 `workbench_agent/api/frontend.py`、`workbench_agent/delivery/dispatcher.py` 和现有迁移资源；从源码目录外清除 PYTHONPATH 后，导入 `workbench_agent.settings`、创建 API 并执行 `dod --help` 与临时数据库迁移。运行该测试，确认在旧包结构下失败。
- [ ] 将实际源码收入 `src/workbench_agent/`，新增 `__init__.py`；`src/workbench_agents.egg-info` 等产物不搬入包。把 Python `web/routes.py` 迁入 `api/frontend.py`，删除空的旧 Python web 包。
- [ ] 改为 `from workbench_agent.domain...` 等导入；同步相对导入、TYPE_CHECKING、测试中的 import、补丁字符串、子进程 `python -c`、`importlib.import_module('connectors.' + module)` 等动态引用。推荐先用 AST/CST 识别 Python 导入，再审阅字符串引用；禁止全仓替换 `tools`、`domain` 等普通单词。
- [ ] 修改 setuptools 包发现和全部 CLI 入口，例如：

```toml
[project.scripts]
dod = "workbench_agent.cli.main:main"
dod-synthetic-policy = "workbench_agent.cli.synthetic_source_policy:main"
dod-synthetic-proposal-policy = "workbench_agent.cli.synthetic_proposal_policy:main"
dod-synthetic-batch-policy = "workbench_agent.cli.synthetic_batch_policy:main"
dod-local-dispatch = "workbench_agent.cli.local_dispatcher:main"

[tool.setuptools]
package-dir = {"" = "src"}

[tool.setuptools.packages.find]
where = ["src"]
namespaces = true
include = ["workbench_agent", "workbench_agent.*"]
```

- [ ] 删除旧 `py-modules = ["settings"]`。保留 namespace 包发现能力以覆盖当前无 `__init__.py` 的 delivery；实测 migrations 资源完整，避免仅看构建成功。
- [ ] 审核 `evaluation/` 是否属于运行时支持能力：CLI 已提供 evaluate，因此保持其可安装性，补充安装态 `dod evaluate` 的现有离线数据集验证。
- [ ] 核查 Alembic `env.py` 与历史版本文件的导入；仅修正 Python 导入路径，不更改 migration revision、DDL 和不可变 schema 快照内容。
- [ ] 运行锁文件一致性检查；若构建元数据要求更新 lock，则使用 uv 正常更新并审阅差异，禁止借机升级依赖。
- [ ] 执行 Agent 全量离线测试与 wheel 安装测试，保留旧 checkpoint 样本，验证新代码能读取并恢复迁移前保存的状态；若状态编码包含旧模块路径，先解决兼容问题再进入下一阶段。
- [ ] 提交：`refactor: namespace Agent Python modules under workbench_agent`。

**通过条件：** 旧目录下源码测试与安装态测试均通过；源码不再依赖通用顶层包名，不增加 `sys.modules` 别名或通用模块转发壳。

## Task 3：解耦前端产物路径

**涉及文件：** 旧位置下的 `src/workbench_agent/settings.py`、`src/workbench_agent/api/frontend.py`、`tests/integration/test_web_assets.py`。

**接口：** 新增 `Settings.frontend_dist_root: Path | None`，环境变量 `WORKBENCH_FRONTEND_DIST_ROOT`；有效目录为显式值，否则沿用 `asset_root / 'web' / 'dist'`。环境变量高于 YAML；路径必须为绝对路径，目录不存在时路由返回现有 503，不要求 Worker 具备前端产物。

- [ ] 先增加参数化行为测试：显式目录可服务页面；未配置时原路径仍工作；相对路径被拒绝；缺失构建返回 503；不存在的资产返回 404；`/v1` 不被 SPA 吞掉；符号链接无法逃逸配置根目录。保留原有测试并运行，确认新增场景先失败。
- [ ] 按既有 Settings 校验模式添加字段与环境变量读取，校验绝对路径但不强制目录存在；不要把 YAML 之外的其他字段无意改为新的优先级规则。
- [ ] 将静态路由的 root 计算改为新字段与兼容默认值的选择，保留解析路径后的边界检查、HTML 深链接判断和缓存策略。
- [ ] 执行 `uv run python -m pytest tests/integration/test_web_assets.py tests/contract/test_package_layout.py`，验证源码与安装态。
- [ ] 提交：`refactor: configure frontend build root independently`。

**通过条件：** 前端源码可以独立搬迁；既有部署配置未设置新变量时行为兼容。

## Task 4：搬迁为项目级多应用目录

**涉及文件：** 路径映射表全部已跟踪项；新增 `projects/dod/.gitignore`、`projects/dod/README.md`；调整 Agent 跨组件测试和打包测试。

**接口：** 两个 Python 应用仍各自安装依赖；Agent 测试中的 MCP fixture 从 `apps/mcp-server/src` 导入本地组件，生产依赖不增加 MCP Server Python 包。

- [ ] 使用 `git ls-files` 清单逐项 `git mv` 已跟踪源码。不能把 `.venv`、node_modules、var、抓包和未跟踪材料随整目录移动。将 Web 独立到 `apps/web`，按映射合并文档及配置；重名文档放入 `docs/mcp-server`。
- [ ] MCP 示例配置改名为 `config/local/mcp-server.example.yaml`；真实本地副本使用 `mcp-server.yaml` 并忽略，不写入真实 token。Agent 配置移为 `config/local/agent.yaml`；工作流放到 `config/workflows`，按调用处修正路径。
- [ ] spec Markdown、HTML、ZIP 和资源一起盘点；修正 Markdown/HTML 资源相对链接。ZIP 作为历史归档保留原内容，标注快照属性，不把 ZIP 中旧路径当运行依赖。
- [ ] 合并忽略规则，覆盖 `**/.venv/`、`**/node_modules/`、`**/__pycache__/`、`**/.pytest_cache/`、`**/*.egg-info/`、`**/build/`、`**/dist/`、`var/`、本地 MCP 配置及抓包；不要忽略正式 example 配置。
- [ ] 集中定义 Agent 测试 fixture（`apps/agent/tests/conftest.py`）：通过 `Path(__file__).resolve().parents[2] / 'mcp-server' / 'src'` 得到 MCP 源码根；断言目录存在，再用 monkeypatch.syspath_prepend。修改 test_dod_mcp_mapping、test_m3_synthetic_journeys、test_qa_code_live、test_multi_graph、test_knowledge_graph 的重复逻辑。
- [ ] 修正 `test_delivery_packaging.py`：Git 根由 `git rev-parse --show-toplevel` 获取，archive 路径由应用目录相对 Git 根计算，wheel 断言前缀改为 `workbench_agent/delivery/`。该测试读取 HEAD：先提交本阶段完整目录变更，再执行最终 archive 验证；验证失败时只修正本阶段并补充提交，禁止跳过或放宽断言。
- [ ] 按新目录重建虚拟环境、执行 npm ci，校验锁文件；不复用旧虚拟环境里的绝对路径入口。
- [ ] 在新位置运行 Agent 离线集成/E2E、MCP 全量测试及 Web 测试/类型检查/构建；记录非预期 skip 为失败。
- [ ] 提交目录迁移：`refactor: organize DoD components under apps`；随后执行 archive 打包检查。

**通过条件：** 已跟踪项目文件全部具有新归属，旧目录仅可能保留明确登记的本地数据；两个 Python 应用各自能构建和运行，跨组件测试实际执行。

## Task 5：修正部署构建及运行资源

**涉及文件：** `projects/dod/deploy/Dockerfile.agent`、新建 `Dockerfile.mcp-server`、`compose.local.yml`、项目根 `.dockerignore`；部署说明 `docs/runbooks/source-layout.md`。

**接口：** Docker build context 统一为 `projects/dod`。Agent 容器 `WORKBENCH_ASSET_ROOT=/opt/dod/assets`、`WORKBENCH_FRONTEND_DIST_ROOT=/opt/dod/web/dist`；配置可挂载 `/config/agent.yaml`。SQLite 持久化路径保持 `/var/lib/dod`，API/Worker/Scheduler 共用相同本地卷。

- [ ] 保留 Agent 当前 Python 镜像 digest、SQLite 源校验和、运行用户、SQLite 下限与停机宽限时间；只调整 COPY 路径并增加前端构建。
- [ ] 增加 Node 22 构建阶段，以 `apps/web/package*.json` 执行 `npm ci`，复制前端源码执行 `npm run build`；镜像锁定实际核验过的 digest，不在方案中虚构 digest。
- [ ] Python wheel 阶段从 `apps/agent` 构建；运行阶段复制 wheel、`config/` 和 Vue dist。运行阶段不复制 node_modules、源码树、测试目录和开发凭据。
- [ ] MCP 镜像从 `apps/mcp-server` 安装自己的冻结依赖和 wheel；启动入口 `workbench-mcp-server`，不依赖 Agent Python 包。
- [ ] Compose 中三个 Agent 服务的构建配置使用 `context: ..`、`dockerfile: deploy/Dockerfile.agent`；配置挂载改为 `../config/local/agent.yaml:/config/agent.yaml:ro`，命令中的 `--config` 同步修改。
- [ ] MCP 放入 `profiles: ["mcp"]`，内部端口 8001，监听 0.0.0.0；配置 allowed_hosts 与调用 URL 匹配。容器间 Agent URL 使用 `http://mcp-server:8001/mcp` 只有在现有安全策略允许时才能使用；当前客户端要求非回环 HTTPS，因此默认不放宽安全策略：本地合成集成走回环，容器跨服务联调使用可信 HTTPS 入口及证书。
- [ ] 不给示例源填入假装可用的生产 URL；启用 MCP profile 前提供本地有效配置、服务身份和可信 HTTPS 路由。无有效配置时，默认 Compose 的三个 Agent 服务仍可检查配置，不宣称诊断链路可用。
- [ ] `.dockerignore` 排除本地运行数据、抓包、缓存、node_modules、venv、dist、本地 MCP 配置；保留构建需要的锁文件、示例配置及前端源码。凭据使用运行时注入，不作为镜像层。
- [ ] 从项目根执行下列验证；构建依赖网络时记录实际结果，不将命令生成等同于构建成功。

```bash
docker compose -f deploy/compose.local.yml config --quiet
docker build -f deploy/Dockerfile.agent -t dod-agent:layout-check .
docker build -f deploy/Dockerfile.mcp-server -t workbench-mcp-server:layout-check .
docker run --rm dod-agent:layout-check --help
docker run --rm workbench-mcp-server:layout-check --help
```

- [ ] 用独立临时卷执行显式 migrate/bootstrap，再验证 API、Worker、Scheduler 与页面资产；MCP profile 在有效测试配置下另验。测试卷禁止复用现有运行数据库。
- [ ] 提交：`build: align DoD images and compose with project layout`。

**通过条件：** 镜像从新根目录构建，安装态 API 能服务 `/app/`，API/Worker/Scheduler 使用正确配置和同一测试数据库；MCP 服务保持独立安装与运行。

## Task 6：统一文档和本地开发命令

**涉及文件：** 项目 README、`apps/web/README.md`、`apps/mcp-server/README.md`、`docs/runbooks/`、现有 spec 和计划中的可执行路径引用。

- [ ] 项目 README 说明三个组件、Python 与 Node 版本、SQLite 版本、锁文件、环境变量和本地启动顺序。历史方案保留日期和当时语境；对旧路径加映射说明，当前运行手册和命令必须使用新路径。
- [ ] MCP README 修正“Agent 适配器尚未实现”的历史表述，明确项目内本地链路已具备实现、企业数据源仍需验收。
- [ ] Vue README 按实际页面连接状态更新，说明独立 Vite 预览和 Agent 静态托管两种运行方式；不把迁移完成写成 M4 全部功能完成。
- [ ] 在 `docs/runbooks/source-layout.md` 固定下列命令。`WORKBENCH_ROOT` 在项目根执行时取当前绝对路径，配置中仍需提供现有应用要求的外部依赖引用；doctor 不通过时先修正配置，不绕过检查。

```bash
# 工作目录：projects/dod
export WORKBENCH_ROOT="$PWD"
export WORKBENCH_ASSET_ROOT="$WORKBENCH_ROOT"
export WORKBENCH_FRONTEND_DIST_ROOT="$WORKBENCH_ROOT/apps/web/dist"

# 以下各组分别在所标目录执行
# apps/agent
uv sync --frozen --group dev
uv run workbench --help
uv run workbench db migrate --config "$WORKBENCH_ROOT/config/local/agent.yaml"
uv run workbench bootstrap --config "$WORKBENCH_ROOT/config/local/agent.yaml"
uv run workbench doctor --config "$WORKBENCH_ROOT/config/local/agent.yaml"
uv run workbench api --config "$WORKBENCH_ROOT/config/local/agent.yaml" --host 127.0.0.1 --port 8000

# 新终端 apps/agent；设置相同环境变量后执行
uv run workbench worker --config "$WORKBENCH_ROOT/config/local/agent.yaml"
# 新终端 apps/agent；设置相同环境变量后执行
uv run workbench scheduler --config "$WORKBENCH_ROOT/config/local/agent.yaml"

# apps/web
npm ci
npm run build
# 独立 UI 开发预览
npm run dev

# apps/mcp-server：先将 example 复制成已忽略的本地配置并配置有效源
uv sync --frozen --group dev
uv run workbench-mcp-server check --config "$WORKBENCH_ROOT/config/local/mcp-server.yaml"
uv run workbench-mcp-server serve --config "$WORKBENCH_ROOT/config/local/mcp-server.yaml" --transport streamable-http
```

- [ ] README 明确 `db migrate`、`bootstrap` 为显式运维动作，不放入每个运行进程的启动脚本；日常启动已有数据库时从 doctor 开始。
- [ ] 搜索整个仓库内旧目录引用，逐个判断并修正活跃文档/脚本；外部已发布页面列出待更新清单。历史对比、迁移映射和 ZIP 快照允许保留旧名称。
- [ ] 提交：`docs: document DoD project development and startup`。

**通过条件：** 开发者能从新目录独立安装、构建与启动；路径、工作目录和真实数据源限制明确。

## Task 7：最终回归、兼容性与交付

**涉及文件：** 新增 `projects/dod/docs/acceptance/2026-11-14-source-migration-result.md`；按发现修正迁移相关文件。

- [ ] Agent 在 `apps/agent` 执行 `uv run python -m pytest`；额外确认 `tests/contract/test_package_layout.py`、`test_delivery_packaging.py`、`test_dod_mcp_mapping.py`、`tests/integration/test_web_assets.py` 和 `tests/e2e/test_m3_synthetic_journeys.py` 的实际执行结果。
- [ ] MCP 在 `apps/mcp-server` 执行完整 pytest、ruff check、ruff format --check；Web 在 `apps/web` 执行 npm test、typecheck、build。
- [ ] 两个 Python 应用分别构建 wheel 并在源码目录外安装测试，清除 PYTHONPATH；对 Agent 验证 API 创建、页面、CLI、迁移资源、evaluate 与 Graph 注册；对 MCP 验证 CLI 和本地协议测试。
- [ ] 使用 Task 1 的迁移前 checkpoint/数据库测试副本进行读取与恢复，检查无 schema 意外升级、无数据丢失、无旧模块路径反序列化失败。生产/现有数据库不直接用于回归。
- [ ] 验证 SSE 重连、恢复执行、工具权限与租户隔离等既有测试未回退；对比通过/失败/skip 清单，禁止减少覆盖来换取绿色。
- [ ] 执行 `git diff --check`；盘点旧路径活动引用；检查未将 venv、node_modules、var、数据库、抓包或凭据加入暂存区。
- [ ] 更新结果文档，记录提交 SHA、环境、命令与真实结果、未执行的 live/容器检查及原因。只有最终提交通过 archive 测试后，才声明迁移完成。
- [ ] 提交结果：`test: verify DoD layout migration compatibility`。依用户现有分支选择在 master 完成；本方案不自动推送远程。

## 验收矩阵

| 检查项 | 通过标准 |
|---|---|
| 源码完整性 | 两个旧组件全部已跟踪源码均有新归属，业务行为没有夹带修改 |
| Python 包 | Agent 使用 workbench_agent；MCP 使用 workbench_mcp_server；无依赖旧通用顶层包的运行路径 |
| 发行包 | 原发行名称与 CLI 保持；源码目录外安装运行成功；迁移资源与静态路由打入 wheel |
| Web | 测试/类型检查/构建通过；/app/、深链接和资源正常；资源越界与 API 路由隔离保留 |
| MCP | 组件测试与 Agent 合成链路实际运行；无因旧路径而 skip；未绕过 HTTPS 和主体权限 |
| 存储兼容 | 旧测试数据/Checkpoint 可读可恢复；revision 和业务 schema 无意外变化 |
| 部署 | 新根目录构建成功；Compose 配置正确；安装态页面可用；独立临时卷验证完成 |
| 文档 | 新开发者可根据 README 启动；真实数据源验收边界准确 |
| Git | 无无关文件或本地敏感数据混入；活动引用已修正；最终提交打包测试通过 |

## 切换与回滚

迁移期间不混用新旧版本进程。执行源码与测试可以分阶段提交；实际运行切换在全部检查完成后进行。

1. 记录原提交、镜像、配置绝对路径和数据库位置；按既有备份手册创建一致性备份。不要复制运行中的 SQLite 单个数据库文件作为有效备份。
2. 停止旧 API、Worker、Scheduler，确认租约任务按现有协议收敛；MCP 若需要换镜像也停止对应进程。
3. 在同一既有数据位置配置新版本，执行 doctor；本轮无 schema 变更，不为目录迁移重置数据库或重新 bootstrap。
4. 启动新 MCP（配置有效时）、API、Worker、Scheduler，检查健康、静态页面及本地受控诊断链路。
5. 若启动失败、Checkpoint 不兼容、页面缺失或权限检查异常，停止新进程，恢复原镜像/代码与原配置，使用同一数据位置重启旧版并检查恢复。回滚前先确认新版本没有产生旧版本不支持的持久化状态；若产生，按既有恢复/隔离流程处理，不能覆盖新数据或重放外部写入。
6. Git 撤销使用迁移提交的反向提交，保留已有无关改动；禁止 `reset --hard` 或删除整个旧目录。已运行系统不因源码回退自动恢复数据库备份。

## 工作量与执行顺序

建议串行执行 Task 1 → 2 → 3 → 4 → 5 → 6 → 7。目录移动、包名和测试路径存在依赖，不并发修改共享目录。

初步估计为 3–5 个工程日：基线与盘点约半天，包名及安装测试约一天，目录/资源/引用约半天至一天，部署与回归约一至两天。实际耗时取决于现有基线、依赖下载和 Docker 可用性；企业真实数据源验收不计入本迁移工期。

计划执行可在当前会话逐项进行；本文件本身不授权远程推送、发布或真实企业数据源操作。

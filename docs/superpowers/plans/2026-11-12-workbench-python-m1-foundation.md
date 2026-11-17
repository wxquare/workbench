---
title: "DoD Agents Python：M1 运行底座实施计划"
date: "2026-11-12"
status: completed
---

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在真实 SQLite 文件上交付受理、领取、检查点、等待/恢复/取消、SSE、结果和故障恢复的可运行闭环。
**Architecture:** 固定响应 LangGraph 验证持久化生命周期，所有存储写入服从同事务 UoW/Guard；不以模型输出证明一致性。
**Tech Stack:** Python 3.12、FastAPI/Pydantic v2、SQLAlchemy 2 async、Alembic、SQLite 3.51.3+、aiosqlite、LangGraph 1.x、uv、pytest。

**完成验收（2026-11-12）：** T01–T09、任务复审和整个分支最终复审均已完成；主流程独立运行完整测试，416 项通过、无警告。源码提交 `51d6b9712062925db9ddd9b7e742e56f7926f2f6`，证据与已接受限制见 [M1 验收记录](../../acceptance/M1.md)。

## Global Constraints

完整约束见[总计划](2026-11-12-workbench-python-implementation-plan.md)。SQLite 单机持久路径、WAL/FULL/FK、5 秒存储期限、同库同事务 Guard、45/10/5 秒租约、2 Run 并发为本阶段硬条件。普通进程不得迁移 Schema，图 thread_id 必须为 run_id。

## 实施任务

### T01：项目入口、配置与启动拒绝规则

**依赖与依据：** 无；规格 章节 3.1、6、18.1/18.2、22.2；AT-32。
**文件：** 创建或修改 `pyproject.toml`、`uv.lock`、`.python-version`、`src/cli/__init__.py`、`src/settings.py`、`src/cli/main.py`、`src/cli/doctor.py`、`config/local.yaml`、`deploy/Dockerfile`、`tests/conftest.py`、`tests/unit/test_settings.py`、`tests/integration/test_storage_readiness.py`、`docs/acceptance/connector-readiness.md`、`tests/contract/test_package_layout.py`、`.gitignore`。
**Interfaces：** Settings.model_validate(data: dict) -> Settings；check_sqlite_runtime(settings: Settings) -> StorageDiagnostics；CLI dod doctor --config PATH。所有进程共享同一配置解析器；cli.main.main() -> None 为 dod 命令入口；Settings.asset_root 从 WORKBENCH_ASSET_ROOT 解析为绝对资源根目录。

- [x] **Step 1：写失败测试及本任务列明的边界用例。**

```python
import pytest
from settings import Settings

@pytest.mark.parametrize("url", [
    "sqlite+aiosqlite:///:memory:",
    "sqlite+aiosqlite:///relative.sqlite3",
])
def test_production_rejects_ephemeral_database(url):
    with pytest.raises(ValueError, match="persistent absolute"):
        Settings.model_validate({
            "environment": "production",
            "storage": {"backend": "sqlite", "database_url": url},
        })
```

- [x] **Step 2：运行测试确认失败。**

```bash
uv run pytest tests/unit/test_settings.py tests/integration/test_storage_readiness.py tests/contract/test_package_layout.py -v
```

预期首次因尚未实现的导入、接口或目标行为失败；夹具自身报错先修复夹具，不能把无关失败当红灯。

- [x] **Step 3：实现最小交付。**

先建立 uv 包和 dod console script，依赖约束按总计划锁定，pytest-asyncio auto、live 标记及 --run-live 开关一起落地。配置从 secret_ref 解析 URL，但诊断只输出脱敏路径与能力。分别测试后端/driver 不符、SQLite 链接库 <3.51.3、PRAGMA 错误、非持久卷、production development 身份、Schema/Codec 不符均拒绝 ready；Python 版本不能代替 sqlite3.sqlite_version 检查。镜像构建安装正确链接库并固定 digest，不能降低规格版本来让宿主机测试通过。doctor 不调用模型与写工具，登记外部依赖表。

额外落实源码布局：`src/` 不创建业务包，新增的 `api`、`runtime`、`persistence`、`cli` 等包各自包含 `__init__.py`。根目录 `.gitignore` 排除 `var/`、构建产物和临时工作目录。按下列配置实现包发现与命令入口；根目录 `skills/`、`prompts/`、`config/`、`web/`、`runner/` 不打进 Python wheel，由部署资源层提供。新增包布局契约测试在临时构建目录生成 wheel、安装到干净环境并切换至项目外目录，验证 `import api`、`import runtime`、`import persistence`、`import skill_management`、`from settings import Settings` 和 `dod --help`；检查 wheel 不含实际 Skill 包，资源加载仅依赖显式 `WORKBENCH_ASSET_ROOT`，不能靠 pytest 的 sys.path 掩盖安装错误。尚未实现的业务包在所属任务创建后加入同一安装回归。

```toml
[build-system]
requires = ["setuptools>=75"]
build-backend = "setuptools.build_meta"

[project.scripts]
dod = "cli.main:main"

[tool.setuptools]
package-dir = {"" = "src"}
py-modules = ["settings"]

[tool.setuptools.packages.find]
where = ["src"]
namespaces = false
```

关键实现约束：

```python
from pathlib import Path
from sqlalchemy.engine import make_url

def require_sqlite_absolute_path(database_url: str) -> Path:
    url = make_url(database_url)
    database = url.database
    if url.drivername != "sqlite+aiosqlite" or not database:
        raise ValueError("backend/driver mismatch")
    if database == ":memory:" or not Path(database).is_absolute():
        raise ValueError("persistent absolute SQLite path required")
    return Path(database).resolve()
```

- [x] **Step 4：执行上述测试命令及已完成的相关契约，确认通过。**

通过条件：错误配置明确拒绝；合法持久路径的诊断返回实际 SQLite 版本、PRAGMA、Schema、Codec，不执行推理。 保留实际执行输出到阶段验收记录。

- [x] **Step 5：检查差异并提交当前任务。**

```bash
git diff --check
git add -- pyproject.toml uv.lock .python-version src/cli/__init__.py src/settings.py src/cli/main.py src/cli/doctor.py config/local.yaml deploy/Dockerfile tests/conftest.py tests/unit/test_settings.py tests/integration/test_storage_readiness.py docs/acceptance/connector-readiness.md tests/contract/test_package_layout.py .gitignore
git diff --cached
git commit -m "chore: bootstrap Python runtime and validated configuration"
```

仅包含本任务交付文件；某项文件未改动时从暂存命令去掉，不夹带其他项目内容。


### T02：SQLite Schema、短事务与统一存储接口

**依赖与依据：** T01；规格 章节 3.5、13、15.1；AT-25/26/32。
**文件：** 创建或修改 `src/persistence/ports/backend.py`、`src/persistence/ports/uow.py`、`src/persistence/ports/stores.py`、`src/persistence/backends/sqlite.py`、`src/persistence/schema.py`、`src/persistence/errors.py`、`src/persistence/migrations/env.py`、`src/persistence/migrations/versions/0001_core.py`、`alembic.ini`、`src/cli/db.py`、`tests/support/storage.py`、`tests/contract/storage/test_uow.py`、`tests/fault/test_sqlite_contention.py`、`src/domain/admin_jobs.py`、`src/jobs/admin.py`、`tests/contract/storage/test_admin_jobs.py`。
**Interfaces：** StorageBackend.create_uow() -> UnitOfWork；UnitOfWork.begin_read()/begin_write() -> AsyncContextManager[UnitOfWork]、commit()/rollback()；StorageClock.now_ms(uow) -> int。RunStore、JobStore、ExecutionLeaseStore、CheckpointStore、RunEventStore、ActionStore、OutboxStore 使用同一 uow 参数。

- [x] **Step 1：写失败测试及本任务列明的边界用例。**

```python
import pytest

@pytest.mark.asyncio
async def test_failed_transaction_rolls_back_all_rows(sqlite_backend):
    with pytest.raises(RuntimeError, match="crash"):
        async with sqlite_backend.create_uow().begin_write() as uow:
            await uow.execute_seed_team("team-a")
            raise RuntimeError("crash")
    async with sqlite_backend.create_uow().begin_read() as uow:
        assert await uow.count_seed_teams() == 0
```

- [x] **Step 2：运行测试确认失败。**

```bash
uv run pytest tests/contract/storage/test_uow.py tests/fault/test_sqlite_contention.py -v
```

预期首次因尚未实现的导入、接口或目标行为失败；夹具自身报错先修复夹具，不能把无关失败当红灯。

- [x] **Step 3：实现最小交付。**

按规格 章节 13.2 建立全部核心表与可移植唯一键/索引，后续任务逐步接通 Store；仅 migrate 可执行 DDL。迁移处理 threads.active_run_id、interrupts.accepted_resume_request_id 的循环引用，不依赖 deferred FK。测试用 tests/support/storage.py 为真实 SQLAlchemy UoW 包装 execute_seed_team/count_seed_teams；仅插入查询专用测试记录，生产 UoW 不增加这两个方法。单元事务测试另覆 commit、rollback、关闭连接和数据库时钟。并发/忙锁测试用两个进程与 Barrier，验证 5 秒总期限内 STORAGE_BUSY、整事务回滚、唯一约束竞争。错误统一为 STORAGE_BUSY/UNAVAILABLE/UNIQUE_CONFLICT/STALE_EXECUTION；磁盘满、I/O 和完整性失败取消 ready。金额不用浮点、JSON 不做核心索引，团队 FK 同时校验归属。 对规格出现但核心表清单未单列的持久化对象，初始Schema补 query_scope_revisions（可信Scope版本）、admin_jobs/admin_job_events（Skill导入/仓库sync/模型与工具probe），不能将不属于Run的管理任务塞入要求run_id的run_jobs。admin_jobs含team/workspace/kind/status/lease/attempt/result_ref/idempotency信息，由独立AdminJobGuard的CAS保护；接口AdminJobService.submit(auth,kind,payload,key)->AdminJob、get(auth,job_id)->AdminJobView，复用对应业务路由返回job_id，无新增通用管理API。

关键实现约束：

```python
from sqlalchemy import text

async def begin_sqlite_write(connection):
    await connection.exec_driver_sql("BEGIN IMMEDIATE")
    return connection

async def storage_now_ms(connection) -> int:
    result = await connection.execute(text(
        "SELECT CAST((julianday('now') - 2440587.5) * 86400000 AS INTEGER)"
    ))
    return int(result.scalar_one())
```

- [x] **Step 4：执行上述测试命令及已完成的相关契约，确认通过。**

通过条件：真实文件和多连接通过原子性与唯一性测试；超时失败不留半笔受理记录。 保留实际执行输出到阶段验收记录。

- [x] **Step 5：检查差异并提交当前任务。**

```bash
git diff --check
git add -- src/persistence/ports/backend.py src/persistence/ports/uow.py src/persistence/ports/stores.py src/persistence/backends/sqlite.py src/persistence/schema.py src/persistence/errors.py src/persistence/migrations/env.py src/persistence/migrations/versions/0001_core.py alembic.ini src/cli/db.py tests/support/storage.py tests/contract/storage/test_uow.py tests/fault/test_sqlite_contention.py src/domain/admin_jobs.py src/jobs/admin.py tests/contract/storage/test_admin_jobs.py
git diff --cached
git commit -m "feat: add portable storage schema and SQLite unit of work"
```

仅包含本任务交付文件；某项文件未改动时从暂存命令去掉，不夹带其他项目内容。


### T03：身份、角色、Workspace 与不可变发布版本

**依赖与依据：** T02；规格 章节 4.3、5、10.3.1、17、18；AT-03/33/34。
**文件：** 创建或修改 `src/domain/identity.py`、`src/domain/context.py`、`src/domain/profiles.py`、`src/api/auth.py`、`src/api/teams.py`、`src/api/workspaces.py`、`src/application/profiles.py`、`src/cli/bootstrap.py`、`src/api/app.py`、`tests/support/platform.py`、`tests/support/profiles.py`、`tests/integration/test_tenant_isolation.py`、`tests/integration/test_model_bootstrap.py`、`tests/integration/test_model_binding.py`。
**Interfaces：** AuthContext(team_id, subject, roles)；ProfileService.publish(auth, workspace_id, expected_revision) -> RuntimeProfile；ContextResolver.resolve(auth, workspace_id) -> RunContext；BootstrapService.apply(settings) -> BootstrapReport。

- [x] **Step 1：写失败测试及本任务列明的边界用例。**

```python
import pytest

@pytest.mark.asyncio
async def test_bootstrap_does_not_replace_published_profile(platform):
    first = await platform.bootstrap()
    await platform.publish_model(model_id="team-approved-model")
    await platform.bootstrap()
    current = await platform.profiles.get_active_model(platform.auth)
    assert current.model_id == "team-approved-model"
    assert first.default_model.name == "deepseek-dod-default"
```

- [x] **Step 2：运行测试确认失败。**

```bash
uv run pytest tests/integration/test_tenant_isolation.py tests/integration/test_model_bootstrap.py tests/integration/test_model_binding.py -v
```

预期首次因尚未实现的导入、接口或目标行为失败；夹具自身报错先修复夹具，不能把无关失败当红灯。

- [x] **Step 3：实现最小交付。**

实现 viewer/operator/maintainer/admin、OIDC Bearer 与 connector 服务身份，team_id 只从认证解析，客户端声明不可覆盖。实现团队/成员和 Workspace 草稿/发布/If-Match，版本软删除或 RESTRICT，审计只追加。bootstrap 以 create_if_missing 种入 DeepSeek @1、基础角色、Workspace 与默认绑定；不解析秘密到持久模型。T11 接通真实模型管理，本任务只落实版本记录和绑定。tests/support/platform.py 定义 platform fixture：auth/other_auth、backend、profiles、bootstrap()、publish_model(model_id)，后续任务显式扩展。client fixture 组合 create_app 与 AsyncClient；测试同团队角色拒绝、跨团队统一 404、成员撤销后即时拒绝和 draft 并发 revision 冲突。 publish_model测试辅助方法发布默认Profile的新模型版本并发布Workspace绑定，两步均走实际发布服务，get_active_model查询Workspace已发布默认绑定。

关键实现约束：

```python
from pydantic import BaseModel, ConfigDict

class VersionRef(BaseModel):
    model_config = ConfigDict(frozen=True)
    resource_id: str
    version: str
    content_hash: str

def resolve_role_model(role: str, default: VersionRef,
                       role_bindings: dict[str, VersionRef]) -> VersionRef:
    return role_bindings.get(role, default)
```

- [x] **Step 4：执行上述测试命令及已完成的相关契约，确认通过。**

通过条件：重复 bootstrap 不改已发布配置；角色/默认解析唯一；跨团队和撤权测试通过。 保留实际执行输出到阶段验收记录。

- [x] **Step 5：检查差异并提交当前任务。**

```bash
git diff --check
git add -- src/domain/identity.py src/domain/context.py src/domain/profiles.py src/api/auth.py src/api/teams.py src/api/workspaces.py src/application/profiles.py src/cli/bootstrap.py src/api/app.py tests/support/platform.py tests/support/profiles.py tests/integration/test_tenant_isolation.py tests/integration/test_model_bootstrap.py tests/integration/test_model_binding.py
git diff --cached
git commit -m "feat: add tenant identity and versioned workspace profiles"
```

仅包含本任务交付文件；某项文件未改动时从暂存命令去掉，不夹带其他项目内容。


### T04：Run 原子受理、Thread 串行与幂等 API

**依赖与依据：** T03；规格 章节 5、11.1–11.4、12.1、13.3；AT-01/21。
**文件：** 创建或修改 `src/domain/runs.py`、`src/application/runs.py`、`src/persistence/idempotency.py`、`src/api/threads.py`、`src/api/runs.py`、`src/api/errors.py`、`tests/support/platform.py`、`tests/support/runs.py`、`tests/integration/test_run_admission.py`。
**Interfaces：** RunService.create(auth: AuthContext, request: CreateRun, idempotency_key: str) -> AcceptedRun；RunService.get(auth, run_id) -> RunView；IdempotencyStore.execute(uow, scope, key, request_hash, operation) -> StoredResponse。

- [x] **Step 1：写失败测试及本任务列明的边界用例。**

```python
import pytest

@pytest.mark.asyncio
async def test_same_key_replays_original_response(client, run_payload):
    first = await client.post("/v1/runs", json=run_payload,
                              headers={"Idempotency-Key": "request-1"})
    replay = await client.post("/v1/runs", json=run_payload,
                               headers={"Idempotency-Key": "request-1"})
    assert first.status_code == replay.status_code == 202
    assert first.json() == replay.json()
    assert replay.headers["Idempotency-Replayed"] == "true"
```

- [x] **Step 2：运行测试确认失败。**

```bash
uv run pytest tests/integration/test_run_admission.py -v
```

预期首次因尚未实现的导入、接口或目标行为失败；夹具自身报错先修复夹具，不能把无关失败当红灯。

- [x] **Step 3：实现最小交付。**

单事务写 Message、Run、versions/RunContext、Job、accepted Event、原始响应/状态码；请求哈希覆盖规范化业务载荷，不包含随机 request_id。唯一约束处理同时到达请求，冲突事务回滚后重读已提交幂等响应。事务提交失败返回 503，同键异载荷 409，键长 1–128，分页 20/100，输入 64 KiB。实现 mode/workflow/Profile 允许范围和预算只能下调；同 Thread 新 Run 可排队但不能抢占 active_run_id。提供 tests/support/runs.py 的 run_payload fixture、platform.create_run(key='r1', thread_id=None, mode='qa')、get_run(run_id)、count_rows(table)，全部经过实际用例。增加提交前失败、提交后响应丢失、72 小时过期、Redis 不可用、自动建 Thread 和重复 client_message_id 的用例。

关键实现约束：

```python
import hashlib
import json

def request_digest(payload: dict) -> str:
    data = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(data).hexdigest()
```

- [x] **Step 4：执行上述测试命令及已完成的相关契约，确认通过。**

通过条件：同请求只有一组 Run/Message/Job/accepted；断线重试查回原 Run；未提交绝不返回 202。 保留实际执行输出到阶段验收记录。

- [x] **Step 5：检查差异并提交当前任务。**

```bash
git diff --check
git add -- src/domain/runs.py src/application/runs.py src/persistence/idempotency.py src/api/threads.py src/api/runs.py src/api/errors.py tests/support/platform.py tests/support/runs.py tests/integration/test_run_admission.py
git diff --cached
git commit -m "feat: persist idempotent run admission and thread routing"
```

仅包含本任务交付文件；某项文件未改动时从暂存命令去掉，不夹带其他项目内容。


### T05：原子领取、LeaseGuard 与 Worker 生命周期

**依赖与依据：** T04；规格 章节 14.4、15、16.2；AT-12/23/26/27。
**文件：** 创建或修改 `src/jobs/claim.py`、`src/jobs/leases.py`、`src/jobs/worker.py`、`src/jobs/fairness.py`、`src/domain/guards.py`、`src/persistence/lease_guard.py`、`src/cli/worker.py`、`tests/support/jobs.py`、`tests/contract/test_execution_guard.py`、`tests/fault/test_guard_takeover_race.py`、`tests/fault/test_sqlite_contention.py`。
**Interfaces：** JobStore.claim(capabilities: WorkerCapabilities, worker_id: str) -> ClaimedJob | None，内含 ExecutionGuard；LeaseGuard.require(uow, guard, allowed_statuses) -> None；LeaseService.renew(guard) -> bool。Guard 固定 thread/run/job/worker/epoch。

- [x] **Step 1：写失败测试及本任务列明的边界用例。**

```python
import pytest
from persistence.errors import StaleExecution

@pytest.mark.asyncio
async def test_old_owner_cannot_append_after_takeover(platform):
    run = await platform.create_run()
    old = await platform.claim(run.run_id, worker_id="worker-a")
    await platform.expire_lease(old)
    new = await platform.reclaim(run.run_id, worker_id="worker-b")
    assert new.run_epoch > old.run_epoch
    with pytest.raises(StaleExecution):
        await platform.events.append_execution(old, "phase.changed", {"phase": "late"})
```

- [x] **Step 2：运行测试确认失败。**

```bash
uv run pytest tests/contract/test_execution_guard.py tests/fault/test_guard_takeover_race.py tests/fault/test_sqlite_contention.py -v
```

预期首次因尚未实现的导入、接口或目标行为失败；夹具自身报错先修复夹具，不能把无关失败当红灯。

- [x] **Step 3：实现最小交付。**

固定顺序：团队额度→Thread→Lease→Run→Job→Interrupt→Action/Outbox，原子 CAS 复核并提交；失败不递增 epoch。实现 45/10/5 秒租约/续约/扫描，默认 2 Run 与团队额度，恢复/回复 > 交互 > 告警 > 定时、同级团队轮转，忙 Thread 延后 1 秒。无 Graph/镜像能力 capability_blocked，续约失败停止新调用；基础设施片段最多 3 次，2/10 秒退避带抖动，不能重发网络动作。T04 实现的 execution Event 写入在此接 Guard，控制/交付事件有独立授权入口。tests/support/jobs.py 增加 claim/expire_lease/reclaim 和独立进程 Barrier，不暴露生产“强制接管”接口。验证 guard 与 takeover 的两个合法串行结果、旧 checkpoint/pending writes/Event/预算/结果全部拒绝。Worker CLI 在此可领取/续约，T06 接执行图。 Worker同时扫描admin_jobs，AdminJobStore.claim(capabilities,worker_id)->ClaimedAdminJob以自己的owner/lease/revision CAS领取，不占业务Thread逻辑位置；AdminJobGuard与Run ExecutionGuard不可混用。管理任务按独立小配额执行且低于人工恢复/在线任务，避免sync/probe堵塞Run。

关键实现约束：

```sql
UPDATE execution_leases
SET guard_revision = guard_revision + 1
WHERE thread_id = :thread_id
  AND owner_run_id = :run_id AND owner_job_id = :job_id
  AND worker_id = :worker_id AND run_epoch = :run_epoch
  AND expires_at > :storage_now_ms;
-- 在同一事务检查 rowcount == 1，再写受保护数据；否则回滚。

```

- [x] **Step 4：执行上述测试命令及已完成的相关契约，确认通过。**

通过条件：每次领取全有或全无；过期旧 Worker 不能在新 epoch 后提交；配额与公平性可观察。 保留实际执行输出到阶段验收记录。

- [x] **Step 5：检查差异并提交当前任务。**

```bash
git diff --check
git add -- src/jobs/claim.py src/jobs/leases.py src/jobs/worker.py src/jobs/fairness.py src/domain/guards.py src/persistence/lease_guard.py src/cli/worker.py tests/support/jobs.py tests/contract/test_execution_guard.py tests/fault/test_guard_takeover_race.py tests/fault/test_sqlite_contention.py
git diff --cached
git commit -m "feat: add atomic job claims and transactional execution fencing"
```

仅包含本任务交付文件；某项文件未改动时从暂存命令去掉，不夹带其他项目内容。


### T06：PortableCheckpointSaver 与可恢复最小图

**依赖与依据：** T05；规格 章节 5.3、7.1、13.6；AT-12/23/25/27。
**文件：** 创建或修改 `src/persistence/checkpoints/codec.py`、`src/persistence/checkpoints/store.py`、`src/runtime/checkpoints/portable.py`、`src/runtime/state/schema.py`、`src/runtime/graphs/registry.py`、`src/runtime/graphs/foundation.py`、`tests/support/checkpoints.py`、`tests/contract/storage/test_checkpoint_saver.py`、`tests/fault/test_sqlite_restart.py`。
**Interfaces：** PortableCheckpointSaver(backend, guard_provider, codec) 实现 BaseCheckpointSaver 五个异步方法；GraphRegistry.resolve(graph_version, state_schema_version) -> CompiledStateGraph；CheckpointStore.put_with_guard(uow, guard, payload) -> CheckpointRef。

- [x] **Step 1：写失败测试及本任务列明的边界用例。**

```python
import pytest

@pytest.mark.asyncio
async def test_checkpoint_restores_pending_writes_after_reopen(platform):
    run = await platform.create_run()
    guard = await platform.claim(run.run_id)
    config = await platform.save_partial_graph(guard, namespace="diagnosis:stable")
    await platform.reopen_database()
    restored = await platform.saver.aget_tuple(config)
    assert restored.pending_writes
    assert restored.parent_config is not None
    assert restored.checkpoint["channel_values"]["run_id"] == run.run_id
```

- [x] **Step 2：运行测试确认失败。**

```bash
uv run pytest tests/contract/storage/test_checkpoint_saver.py tests/fault/test_sqlite_restart.py tests/fault/test_guard_takeover_race.py -v
```

预期首次因尚未实现的导入、接口或目标行为失败；夹具自身报错先修复夹具，不能把无关失败当红灯。

- [x] **Step 3：实现最小交付。**

按锁定 LangGraph 版本读取 BaseCheckpointSaver 接口与特殊写索引规则，先写对照契约再实现；保存 type tag/bytes/metadata/parent_config/pending writes/task_path/codec_version，namespace_hash 与原文冲突报错。alist 覆盖 filter/before/limit/顺序，adelete_thread 限授权且禁止活动 Run。Codec 只接受发布支持类型，拒绝任意可执行反序列化，2 MiB 状态限额。foundation 图只含确定性 prepare→input/compose→end，configurable.thread_id=run_id；Worker 通过 Registry 执行与恢复，不调用旧 Smart/Claude 当新图。tests/support/checkpoints.py 增加 save_partial_graph、reopen_database、saver；真实 LangGraph 并行分支部分完成后关库重开，验证消息稳定 ID、父链与特殊 pending write 去重。T07 接用户恢复，T08 接最终业务投影。

关键实现约束：

```python
import hashlib

def checkpoint_namespace_key(namespace: str) -> str:
    return hashlib.sha256(namespace.encode("utf-8")).hexdigest()

async def save_guarded_checkpoint(backend, guard, payload):
    async with backend.create_uow().begin_write() as uow:
        await uow.lease_guard.require(uow, guard, {"running", "finalizing"})
        return await uow.checkpoints.put(uow, payload)
```

- [x] **Step 4：执行上述测试命令及已完成的相关契约，确认通过。**

通过条件：关闭全部连接后同一图从真实文件继续；旧 guard 的 aput/aput_writes 同事务拒绝。 保留实际执行输出到阶段验收记录。

- [x] **Step 5：检查差异并提交当前任务。**

```bash
git diff --check
git add -- src/persistence/checkpoints/codec.py src/persistence/checkpoints/store.py src/runtime/checkpoints/portable.py src/runtime/state/schema.py src/runtime/graphs/registry.py src/runtime/graphs/foundation.py tests/support/checkpoints.py tests/contract/storage/test_checkpoint_saver.py tests/fault/test_sqlite_restart.py
git diff --cached
git commit -m "feat: implement guarded portable LangGraph checkpoints"
```

仅包含本任务交付文件；某项文件未改动时从暂存命令去掉，不夹带其他项目内容。


### T07：具名中断、恢复去重、取消与执行期限

**依赖与依据：** T06；规格 章节 8.3、10.4、11.5/11.6、14；AT-06/13/14/16/26。
**文件：** 创建或修改 `src/application/resume.py`、`src/application/cancel.py`、`src/runtime/graphs/interrupts.py`、`src/jobs/deadlines.py`、`src/domain/transitions.py`、`tests/support/interrupts.py`、`tests/integration/test_resume.py`、`tests/integration/test_query_scope.py`、`tests/fault/test_cancel_race.py`、`tests/fault/test_resume_replay.py`。
**Interfaces：** RunService.resume(auth, run_id, request: ResumeRun, idempotency_key) -> AcceptedRun；cancel(auth, run_id, key) -> RunView；ResumeApplier.apply(resume_request, checkpoint) -> ResumeDecision；QueryScopeRevision 只由 ContextResolver 创建。

- [x] **Step 1：写失败测试及本任务列明的边界用例。**

```python
import asyncio
import pytest

@pytest.mark.asyncio
async def test_only_one_resume_is_accepted(platform):
    run, interrupt = await platform.waiting_run()
    results = await asyncio.gather(
        platform.resume(run, interrupt, key="answer-a", values={"region": "ID"}),
        platform.resume(run, interrupt, key="answer-b", values={"region": "PH"}),
        return_exceptions=True,
    )
    assert sum(platform.is_accepted(result) for result in results) == 1
    assert await platform.resume_request_count(interrupt.interrupt_id) == 1
```

- [x] **Step 2：运行测试确认失败。**

```bash
uv run pytest tests/integration/test_resume.py tests/integration/test_query_scope.py tests/fault/test_cancel_race.py tests/fault/test_resume_replay.py -v
```

预期首次因尚未实现的导入、接口或目标行为失败；夹具自身报错先修复夹具，不能把无关失败当红灯。

- [x] **Step 3：实现最小交付。**

校验 actor/revision/期限/具名 interrupt/response Schema；先存请求，再 CAS accepted 指针，再保存输入与新 generation Job；同键重放、并发 loser 409。图输入节点只写状态，保存 last_applied_resume_id 检查点前不执行写工具，恢复识别已应用不重复 Command(resume)。waiting 释放执行租约/槽位保留 Thread active_run_id。取消 queued/waiting 原子终结，running 进入 cancel_requested、停止新模型/工具并取消子进程，晚结果不覆盖终态；finalizing 返回 FINALIZATION_STARTED。实现 24h 等待、48h 墙钟、30min 审批及 active_elapsed_ms；有效 Outcome 才 partial。tests/support/interrupts.py 定义 waiting_run/resume/is_accepted/resume_request_count。补测重复回答、过期、审批拒绝、参数哈希变更、取消竞争及 Scope revision，不按“最新消息”猜等待点。

关键实现约束：

```sql
UPDATE interrupts
SET accepted_resume_request_id = :resume_request_id,
    status = 'response_submitted'
WHERE interrupt_id = :interrupt_id AND status = 'open'
  AND accepted_resume_request_id IS NULL;
-- 同事务插入 ResumeRequest；rowcount!=1 回滚，绝不创建第二个 generation。

```

- [x] **Step 4：执行上述测试命令及已完成的相关契约，确认通过。**

通过条件：中断最多一条有效回复；恢复保持 run_id/版本/预算；取消与终态不能回退。 保留实际执行输出到阶段验收记录。

- [x] **Step 5：检查差异并提交当前任务。**

```bash
git diff --check
git add -- src/application/resume.py src/application/cancel.py src/runtime/graphs/interrupts.py src/jobs/deadlines.py src/domain/transitions.py tests/support/interrupts.py tests/integration/test_resume.py tests/integration/test_query_scope.py tests/fault/test_cancel_race.py tests/fault/test_resume_replay.py
git diff --cached
git commit -m "feat: add durable interrupts resume cancellation and deadlines"
```

仅包含本任务交付文件；某项文件未改动时从暂存命令去掉，不夹带其他项目内容。


### T08：最终结果投影、持久事件与 SSE 续传

**依赖与依据：** T07；规格 章节 8.1、11.4、12.2/12.3、15.3；AT-03/15/21/29。
**文件：** 创建或修改 `src/application/results.py`、`src/persistence/events.py`、`src/api/events.py`、`src/api/artifacts.py`、`src/runtime/streaming.py`、`tests/support/events.py`、`tests/integration/test_finalize.py`、`tests/integration/test_sse_replay.py`、`tests/integration/test_sse_wal.py`。
**Interfaces：** ResultService.finalize(guard: ExecutionGuard, outcome: RunOutcome, digest: str) -> RunView；RunEventStore.append_execution(guard,type,payload)/append_control(auth,run_id,type,payload)/append_delivery(action_guard,type,payload) -> RunEvent；read_batch(auth,run_id,after_seq,limit) -> list[RunEvent]。 RunEventStore.is_terminal_and_drained(auth,run_id,after_seq)->bool；wait_for_hint_or_timeout(run_id,seconds)->None，均在返回前释放数据库事务。

- [x] **Step 1：写失败测试及本任务列明的边界用例。**

```python
import pytest

@pytest.mark.asyncio
async def test_finalization_is_idempotent_and_events_replay(platform):
    run, guard, outcome = await platform.finalizable_run()
    await platform.results.finalize(guard, outcome, digest=outcome.digest())
    await platform.results.finalize(guard, outcome, digest=outcome.digest())
    events = await platform.read_events(run.run_id, after_seq=0)
    terminal = [event for event in events if event.type == "run.completed"]
    assert len(terminal) == 1
    assert [event.seq for event in events] == sorted({event.seq for event in events})
```

- [x] **Step 2：运行测试确认失败。**

```bash
uv run pytest tests/integration/test_finalize.py tests/integration/test_sse_replay.py tests/integration/test_sse_wal.py tests/integration/test_tenant_isolation.py -v
```

预期首次因尚未实现的导入、接口或目标行为失败；夹具自身报错先修复夹具，不能把无关失败当红灯。

- [x] **Step 3：实现最小交付。**

finalize 同事务写 Outcome、必要 Incident、Action/Outbox、终态和最终 Event；稳定结果键+digest 保证重复完成返回原结果，终态重复调用走授权读取而非重新拿失效执行锁，不同摘要冲突。M1 无业务动作的 Outcome 允许空 Incident/Outbox，M3 接通。单事务 CAS 更新 last_event_seq 并插入；status revision 不随 delta 改变。实现全部事件枚举、attempt/message_id/superseded、final独立消息及 schema_version。SSE 严格按已提交 seq，处理 Last-Event-ID/after_seq、跨 run 游标拒绝、7d 过期410、15s注释心跳、100条慢缓冲、250ms/256字符批量持久化、Redis失效DB轮询。每批关闭事务再发送，慢客户端不可钉住 WAL；断线不取消，终态排空关闭，后续投递 GET/再订阅。Artifact URL 签发前重授权。tests/support/events.py 增加 finalizable_run/read_events 和真实流读取客户端；SSE 是传输测试，不读取检查点代替事件。

关键实现约束：

```python
async def stream_events(events, auth, run_id, after_seq):
    while True:
        batch = await events.read_batch(auth, run_id, after_seq, limit=100)
        # read_batch 已完成并关闭读事务，网络发送不持有连接。
        for event in batch:
            yield event.to_sse()
            after_seq = event.seq
        if await events.is_terminal_and_drained(auth, run_id, after_seq):
            return
        await events.wait_for_hint_or_timeout(run_id, seconds=1)
```

- [x] **Step 4：执行上述测试命令及已完成的相关契约，确认通过。**

通过条件：最终业务事务只提交一次；断线从任意已提交游标续传；慢客户端下数据库读事务及时结束。 保留实际执行输出到阶段验收记录。

- [x] **Step 5：检查差异并提交当前任务。**

```bash
git diff --check
git add -- src/application/results.py src/persistence/events.py src/api/events.py src/api/artifacts.py src/runtime/streaming.py tests/support/events.py tests/integration/test_finalize.py tests/integration/test_sse_replay.py tests/integration/test_sse_wal.py
git diff --cached
git commit -m "feat: persist final outcomes and replayable SSE events"
```

仅包含本任务交付文件；某项文件未改动时从暂存命令去掉，不夹带其他项目内容。


### T09：投影协调、进程故障、备份恢复与 M1 演示

**依赖与依据：** T08；规格 章节 6、15.3、16、18、22；AT-12–16/21/25/28。
**文件：** 创建或修改 `src/jobs/reconciler.py`、`src/jobs/scheduler.py`、`src/cli/scheduler.py`、`src/cli/backup.py`、`src/persistence/backends/sqlite_backup.py`、`deploy/compose.local.yml`、`tests/support/processes.py`、`tests/fault/test_worker_recovery.py`、`tests/fault/test_interrupt_projection.py`、`tests/fault/test_redis_outage.py`、`tests/fault/test_backup_restore.py`、`docs/runbooks/sqlite-recovery.md`、`docs/acceptance/M1.md`。
**Interfaces：** Reconciler.recover(run_id) -> RecoveryDecision；Scheduler.tick() -> TickReport；SQLiteBackup.create(destination: Path) -> BackupManifest；restore(source: Path, target: Path) -> RestoreReport。管理备份 CLI dod db backup/restore 为本计划新增运维命令。

- [x] **Step 1：写失败测试及本任务列明的边界用例。**

```python
import pytest

@pytest.mark.asyncio
async def test_interrupt_projection_recovered_without_new_question(platform):
    run = await platform.create_run()
    await platform.crash_at(run.run_id, "after_interrupt_checkpoint")
    await platform.recover_after_lease_expiry(run.run_id)
    view = await platform.get_run(run.run_id)
    assert view.status == "waiting_input"
    assert await platform.interrupt_count(run.run_id) == 1
    assert await platform.question_generation_count(run.run_id) == 1
```

- [x] **Step 2：运行测试确认失败。**

```bash
uv run pytest tests/fault/test_worker_recovery.py tests/fault/test_interrupt_projection.py tests/fault/test_resume_replay.py tests/fault/test_redis_outage.py tests/fault/test_backup_restore.py tests/fault/test_sqlite_restart.py -v
```

预期首次因尚未实现的导入、接口或目标行为失败；夹具自身报错先修复夹具，不能把无关失败当红灯。

- [x] **Step 3：实现最小交付。**

协调器覆盖规格 章节 15.3 五种 checkpoint/业务投影组合，先以新 epoch 安全接管，再幂等补 waiting/applied/finalize/recover；不可解释版本/状态失败 STATE_RECONCILIATION_REQUIRED。tests/support/processes.py 通过具名故障钩子在边界暂停子进程后 kill，不在生产暴露故障 HTTP。扩展 crash_at/recover_after_lease_expiry/interrupt_count/question_generation_count；foundation 的确定性问题也计一次节点调用。验证真实进程 SIGKILL、恢复后无重复输入、最终结果/草稿 superseded、Redis全断、连接失败、缺版本 capability_blocked。API关闭15s、Worker30s、Dispatcher提交后确认、Runner组终止在对应进程实现；未完成不伪成功。备份使用 Online Backup，restore 检查 integrity/FK/父链/pending writes/Artifact 引用并默认禁止 Dispatcher，M3补 Action 对账。按 章节 18.2 实际启动 api/worker/scheduler，完成 M1 固定图演示、doctor 和恢复≤90s记录；dispatcher/runner 在 T14/T19落地后复验全命令。

关键实现约束：

```python
import sqlite3

def online_backup(source_path, backup_path):
    source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
    target = sqlite3.connect(backup_path)
    try:
        source.backup(target)
        result = target.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise RuntimeError(result)
    finally:
        target.close()
        source.close()
```

- [x] **Step 4：执行上述测试命令及已完成的相关契约，确认通过。**

通过条件：真实文件进程重启与一致备份恢复通过；M1 AT 记录能复现，未接真实模型/动作明确标明。 保留实际执行输出到阶段验收记录。

- [x] **Step 5：检查差异并提交当前任务。**

```bash
git diff --check
git add -- src/jobs/reconciler.py src/jobs/scheduler.py src/cli/scheduler.py src/cli/backup.py src/persistence/backends/sqlite_backup.py deploy/compose.local.yml tests/support/processes.py tests/fault/test_worker_recovery.py tests/fault/test_interrupt_projection.py tests/fault/test_redis_outage.py tests/fault/test_backup_restore.py docs/runbooks/sqlite-recovery.md docs/acceptance/M1.md
git diff --cached
git commit -m "feat: reconcile interrupted runs and verify SQLite recovery"
```

仅包含本任务交付文件；某项文件未改动时从暂存命令去掉，不夹带其他项目内容。

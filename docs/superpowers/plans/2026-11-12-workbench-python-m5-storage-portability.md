---
title: "DoD Agents Python：M5 后续存储适配实施计划"
date: "2026-11-12"
status: deferred-after-p0
---

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** SQLite首版完成后，实现MySQL/PostgreSQL后端和保留执行语义的跨库迁移。
**Architecture:** 公共Repository/UoW/Guard/Saver契约保持不变，方言责任封闭在Backend；迁移以停写维护窗口、版本化逻辑包、逐字节校验与动作对账完成。
**Tech Stack:** MySQL 8.4 LTS/asyncmy、PostgreSQL 16+/psycopg、SQLAlchemy 2 async、Alembic、同一PortableCheckpointSaver与pytest契约集。

## Global Constraints

遵守[总计划](2026-11-12-workbench-python-implementation-plan.md)。本阶段不属于SQLite首版发布范围。不能仅替换URL、用最终消息替代检查点、令两个后端同时执行同Run，或在目标产生新写入后直接回指旧SQLite文件。

## 实施任务

### T25：MySQL Backend 与同一存储契约集

**依赖与依据：** T24；按后续阶段单独排期；规格 章节 3.5、13.5、15、21.3；AT-30。
**文件：** 创建或修改 `pyproject.toml`、`uv.lock`、`src/persistence/backends/mysql.py`、`src/persistence/migrations/versions/0005_mysql_dialect.py`、`deploy/compose.storage-tests.yml`、`tests/conftest.py`、`tests/support/storage_backends.py`、`tests/contract/storage/test_backend_matrix.py`、`tests/integration/test_mysql_recovery.py`、`docs/acceptance/M5-mysql.md`、`tests/support/storage_contracts.py`。
**Interfaces：** MySQLBackend实现既有StorageBackend/UnitOfWork/StorageClock与所有Store；通过backend注册表选择，不改业务服务签名。测试选项 --storage-backend=sqlite|mysql|postgresql，测试URL由WORKBENCH_TEST_DATABASE_URL注入。

- [ ] **Step 1：写失败测试及本任务列明的边界用例。**

```python
import pytest

@pytest.mark.parametrize("backend_name", ["sqlite", "mysql"])
@pytest.mark.asyncio
async def test_claim_guard_saver_contract_is_identical(storage_backend_factory,
                                                      backend_name):
    backend = await storage_backend_factory(backend_name)
    report = await storage_backend_factory.contract_runner(backend).claim_takeover_and_restore()
    assert report.duplicate_claims == 0
    assert report.stale_writes_committed == 0
    assert report.pending_writes_restored is True
```

- [ ] **Step 2：运行测试确认失败。**

```bash
uv run pytest tests/contract/storage tests/contract/test_execution_guard.py tests/integration/test_mysql_recovery.py tests/integration/test_expert_budget.py tests/integration/test_usage.py tests/integration/test_resume.py tests/integration/test_finalize.py tests/integration/test_sse_replay.py tests/fault/test_action_unknown.py --storage-backend=mysql -v
```

预期首次因尚未实现的导入、接口或目标行为失败；夹具自身报错先修复夹具，不能把无关失败当红灯。

- [ ] **Step 3：实现最小交付。**

MySQL 8.4 LTS/InnoDB/READ COMMITTED，asyncmy连接池，固定对象锁顺序与事务内Guard更新，先实现CAS基线再按测量决定skip-locked；不能只换URL。UUID/alias机器键二进制比较，长索引hash+原文验证，JSON LONGTEXT、typed bytes LONGBLOB、金额整数。测试方言DDL隐式提交下迁移维护窗口/中断恢复，不用SQLite batch当MySQL迁移。tests/support/storage_backends.py的contract_runner仅组合已有shared contract测试场景，不另实现业务；将原Repository/Job/Lease/Guard/Saver/预算/Resume/Event/Action幂等契约参数化运行同集，包括aput_writes特殊索引与多进程takeover Barrier。模拟死锁/连接断开只重试纯DB事务且受5s期限；服务端Clock不能用Worker本地时钟。生产doctor增加服务版本/隔离/URL/backend一致性检查。容量单独验证20活动Run及连接池峰值，模型等待期间不能长期占连接，不以目标值当已实现。 在tests/conftest.py为通用platform和storage_backend注入--storage-backend选择；sqlite_backend保留为只验证SQLite方言的fixture，标记sqlite专属，不能计作MySQL共享契约通过。storage_backend_factory从compose启动三种真实引擎并分别读取WORKBENCH_TEST_MYSQL_URL/WORKBENCH_TEST_POSTGRESQL_URL；只输出脱敏连接信息。每个Backend contract_runner由tests/support/storage_contracts.py（本任务新增）组合生产服务，实现claim_takeover_and_restore并返回独立断言结果；不是后端生产属性。

关键实现约束：

```python
def mysql_machine_key_type(length: int):
    from sqlalchemy.dialects.mysql import VARCHAR
    return VARCHAR(length=length, collation="utf8mb4_bin")
```

- [ ] **Step 4：执行上述测试命令及已完成的相关契约，确认通过。**

通过条件：同一完整共享契约在MySQL通过且故障恢复记录齐全，SQLite回归不退化；独立记录20并发是否达到目标。 保留实际执行输出到阶段验收记录。

- [ ] **Step 5：检查差异并提交当前任务。**

```bash
git diff --check
git add -- pyproject.toml uv.lock src/persistence/backends/mysql.py src/persistence/migrations/versions/0005_mysql_dialect.py deploy/compose.storage-tests.yml tests/conftest.py tests/support/storage_backends.py tests/contract/storage/test_backend_matrix.py tests/integration/test_mysql_recovery.py docs/acceptance/M5-mysql.md tests/support/storage_contracts.py
git diff --cached
git commit -m "feat: add a contract compatible MySQL storage backend"
```

仅包含本任务交付文件；某项文件未改动时从暂存命令去掉，不夹带其他项目内容。


### T26：PostgreSQL Backend 与三后端一致性

**依赖与依据：** T25；规格 章节 3.5、13.5、15、21.3；AT-30。
**文件：** 创建或修改 `pyproject.toml`、`uv.lock`、`src/persistence/backends/postgresql.py`、`src/persistence/migrations/versions/0006_postgresql_dialect.py`、`tests/support/storage_backends.py`、`tests/contract/storage/test_backend_matrix.py`、`tests/integration/test_postgresql_recovery.py`、`docs/acceptance/M5-postgresql.md`。
**Interfaces：** PostgreSQLBackend实现同一StorageBackend/UnitOfWork/StorageClock/Store；PortableCheckpointSaver继续使用公共CheckpointStore，无业务侧advisory lock或原生Saver替换。

- [ ] **Step 1：写失败测试及本任务列明的边界用例。**

```python
import pytest

@pytest.mark.asyncio
async def test_postgresql_rejects_stale_pending_writes(postgresql_platform):
    run = await postgresql_platform.create_run()
    old = await postgresql_platform.claim(run.run_id, worker_id="a")
    await postgresql_platform.expire_lease(old)
    current = await postgresql_platform.reclaim(run.run_id, worker_id="b")
    report = await postgresql_platform.try_stale_checkpoint_and_pending_writes(old)
    assert report.checkpoint_error == "STALE_EXECUTION"
    assert report.pending_write_error == "STALE_EXECUTION"
    assert current.run_epoch > old.run_epoch
```

- [ ] **Step 2：运行测试确认失败。**

```bash
uv run pytest tests/contract/storage tests/contract/test_execution_guard.py tests/integration/test_postgresql_recovery.py tests/integration/test_expert_budget.py tests/integration/test_usage.py tests/integration/test_resume.py tests/integration/test_finalize.py tests/integration/test_sse_replay.py tests/fault/test_action_unknown.py --storage-backend=postgresql -v
```

预期首次因尚未实现的导入、接口或目标行为失败；夹具自身报错先修复夹具，不能把无关失败当红灯。

- [ ] **Step 3：实现最小交付。**

PostgreSQL 16+/psycopg异步池/READ COMMITTED，BLOB映射BYTEA，规范JSON仍TEXT、UUID仍规范字符串。Guard靠同租约行条件更新串行化，不引入session锁/长专用连接，不让业务依赖RETURNING/JSONB/原生UUID/部分唯一索引。用相同测试factory和fixture重跑SQLite/MySQL/PostgreSQL全部Repository、claim、fence、Saver、resume、预算与Outbox契约；postgresql_platform由同Platform组合不同backend产生，新增try_stale_checkpoint_and_pending_writes仅包装生产Saver错误结果。验证死锁/断连/旧写与接管并发、隔离级别/版本错误ready、checkpoint父链/pendingwrites/Codec恢复一致，容量另跑20Run。适配器方言迁移编号是计划建议，实施时按已存在Alembic revision链顺延，不重写已有revision。 同一命令的通用契约部分再以--storage-backend=sqlite运行并记录；实际MySQL/PG用例不得因环境缺失跳过后宣称通过。

关键实现约束：

```sql
SELECT CAST(EXTRACT(EPOCH FROM clock_timestamp()) * 1000 AS BIGINT);
-- 仅适配器内实现StorageClock，事务中的租约判断使用该时钟。

```

- [ ] **Step 4：执行上述测试命令及已完成的相关契约，确认通过。**

通过条件：三个后端同集通过并记录差异仅为性能/方言；不能凭SQLite通过而跳过PG实际竞争与恢复。 保留实际执行输出到阶段验收记录。

- [ ] **Step 5：检查差异并提交当前任务。**

```bash
git diff --check
git add -- pyproject.toml uv.lock src/persistence/backends/postgresql.py src/persistence/migrations/versions/0006_postgresql_dialect.py tests/support/storage_backends.py tests/contract/storage/test_backend_matrix.py tests/integration/test_postgresql_recovery.py docs/acceptance/M5-postgresql.md
git diff --cached
git commit -m "feat: add a contract compatible PostgreSQL storage backend"
```

仅包含本任务交付文件；某项文件未改动时从暂存命令去掉，不夹带其他项目内容。


### T27：停写逻辑迁移、恢复和跨库对账

**依赖与依据：** T26；规格 章节 13.6、16.5、21.3；AT-31。
**文件：** 创建或修改 `src/persistence/transfer/manifest.py`、`src/persistence/transfer/exporter.py`、`src/persistence/transfer/importer.py`、`src/persistence/transfer/validator.py`、`src/cli/transfer.py`、`tests/integration/test_storage_transfer.py`、`tests/fault/test_transfer_recovery.py`、`docs/runbooks/storage-transfer.md`、`docs/acceptance/M5.md`、`tests/support/transfer.py`。
**Interfaces：** StorageTransfer.export(source, destination, maintenance_token) -> TransferManifest；import_bundle(target, bundle, maintenance_token) -> ImportReport；validate(source_manifest, target) -> ValidationReport。CLI dod db export/import/validate明确source/target，不复用普通启动换URL。

- [ ] **Step 1：写失败测试及本任务列明的边界用例。**

```python
import pytest

@pytest.mark.parametrize("target_backend", ["mysql", "postgresql"])
@pytest.mark.asyncio
async def test_migration_preserves_waiting_resume_and_unknown_action(
        transfer_fixture, target_backend):
    source = await transfer_fixture.sqlite_with_waiting_and_unknown_action()
    imported = await transfer_fixture.transfer(source, target_backend)
    assert imported.manifest.ids_preserved
    assert imported.manifest.checkpoint_bytes_preserved
    assert imported.manifest.pending_writes_preserved
    await imported.resume_named_interrupt()
    assert imported.resume_application_count == 1
    assert imported.unknown_action_resend_count == 0
    assert imported.sse_sequence_is_continuous
```

- [ ] **Step 2：运行测试确认失败。**

```bash
uv run pytest tests/integration/test_storage_transfer.py tests/fault/test_transfer_recovery.py -v
```

预期首次因尚未实现的导入、接口或目标行为失败；夹具自身报错先修复夹具，不能把无关失败当红灯。

- [ ] **Step 3：实现最小交付。**

实现维护令牌与停新受理/派发状态，等待短事务结束，停止Worker/Dispatcher；先一致备份SQLite。逻辑包记录schema/codec/graph版本、表/行数/摘要、ID/FK、Scope、事件seq、Action幂等/unknown、完整typedbytes/namespace/父链/pendingwrites，以及admin_jobs/workflow/migration_routes等补充表。不是SQL dump或最终消息导出。目标Schema/Codec相同，按FK顺序导入，active_run_id与accepted_resume_request_id等循环指针先NULL后回填并验引用。清旧owner/失效租约，历史epoch/generation保留，新执行按受控接管创建新epoch与generation；不能在源目标同时继续同Run。import可从受控阶段恢复，不把半导入库声明ready。逐表/逐字节sha/FK/父链/Codec解码校验，真实waiting恢复一次、SSE续传、未投递Outbox和幂等重试；unknown先对账不可自动重发。目标产生新写后禁止直接指回旧库，需停写反向迁移或事实对账；旧SQLite只读。tests/support/transfer.py（新增）定义transfer_fixture和报告，使用真正三种引擎，故障点覆盖半包/缺文件/版本不符/回填中断/目标已写回退拒绝。

关键实现约束：

```python
import base64
import hashlib

def encode_typed_bytes(type_tag: str, payload: bytes) -> dict:
    return {"type": type_tag,
            "bytes_base64": base64.b64encode(payload).decode("ascii"),
            "sha256": hashlib.sha256(payload).hexdigest()}

def decode_typed_bytes(record: dict) -> tuple[str, bytes]:
    payload = base64.b64decode(record["bytes_base64"], validate=True)
    if hashlib.sha256(payload).hexdigest() != record["sha256"]:
        raise ValueError("checkpoint byte digest mismatch")
    return record["type"], payload
```

- [ ] **Step 4：执行上述测试命令及已完成的相关契约，确认通过。**

通过条件：SQLite→MySQL、SQLite→PG两条迁移和恢复通过；ID/seq/typed bytes/父链/pending writes/幂等/unknown均保留，手册含回退限制。 保留实际执行输出到阶段验收记录。

- [ ] **Step 5：检查差异并提交当前任务。**

```bash
git diff --check
git add -- src/persistence/transfer/manifest.py src/persistence/transfer/exporter.py src/persistence/transfer/importer.py src/persistence/transfer/validator.py src/cli/transfer.py tests/integration/test_storage_transfer.py tests/fault/test_transfer_recovery.py docs/runbooks/storage-transfer.md docs/acceptance/M5.md tests/support/transfer.py
git diff --cached
git commit -m "feat: migrate portable storage with checkpoint and action validation"
```

仅包含本任务交付文件；某项文件未改动时从暂存命令去掉，不夹带其他项目内容。

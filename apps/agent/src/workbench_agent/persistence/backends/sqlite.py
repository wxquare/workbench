"""Async SQLite adapter. No runtime DDL, no hidden transaction retries."""
from contextlib import asynccontextmanager
import sqlite3
import asyncio
import time
from sqlalchemy import event, text, URL
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.exc import DBAPIError
from workbench_agent.settings import require_sqlite_absolute_path, MIN_SQLITE_VERSION
from workbench_agent.persistence.errors import StorageError

class SQLiteClock:
    async def now_ms(self,uow):
        row = await uow.execute(text("SELECT CAST((julianday('now') - 2440587.5) * 86400000 AS INTEGER)"))
        return int(row.scalar_one())

class SQLiteBackend:
    capabilities = frozenset({"short_transactions","cas","typed_bytes","database_clock"})

    def __init__(self,database_url:str,*,operation_timeout:float=5,tool_permissions=None):
        if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:
            raise StorageError("STORAGE_UNAVAILABLE")
        if not 0 < operation_timeout <= 5:
            raise ValueError("storage deadline must be within 5 seconds")
        self.path = require_sqlite_absolute_path(database_url)
        self.operation_timeout = operation_timeout
        self._tool_permissions=tool_permissions
        self._ready = True
        # mode=rw forbids ordinary startup from even creating an empty DB file.
        url = URL.create("sqlite+aiosqlite",database=f"file:{self.path}",query={"mode":"rw","uri":"true"})
        self.engine = create_async_engine(url,poolclass=NullPool,connect_args={"timeout":0})
        @event.listens_for(self.engine.sync_engine,"connect")
        def configure(connection, _):
            connection.isolation_level = None
            cursor = connection.cursor()
            try:
                for pragma in ("foreign_keys=ON","synchronous=FULL","wal_autocheckpoint=1000"):
                    cursor.execute(f"PRAGMA {pragma}")
                cursor.execute("PRAGMA journal_mode")
                if cursor.fetchone()[0] != "wal":
                    raise StorageError("STORAGE_UNAVAILABLE")
                cursor.execute("PRAGMA busy_timeout=5000")
            finally:
                cursor.close()
        self.clock = SQLiteClock()
        from workbench_agent.persistence.backends.admin_jobs import SQLiteAdminJobStore
        self.admin_jobs = SQLiteAdminJobStore(self.clock)
        from workbench_agent.persistence.backends.configuration import SQLiteConfigurationStore
        self.configuration = SQLiteConfigurationStore()
        from workbench_agent.persistence.backends.runs import SQLiteRunStore
        self.runs = SQLiteRunStore()
        from workbench_agent.persistence.lease_guard import LeaseGuard
        from workbench_agent.persistence.backends.execution_events import SQLiteExecutionEventStore
        from workbench_agent.persistence.backends.jobs import SQLiteJobStore
        self.lease_guard = LeaseGuard(self.clock)
        self.execution_events = SQLiteExecutionEventStore(self.clock, self.lease_guard)
        self.jobs = SQLiteJobStore(self.clock, self.lease_guard, self.execution_events)
        from workbench_agent.persistence.checkpoints.store import CheckpointStore
        self.checkpoints = CheckpointStore(self)
        from workbench_agent.persistence.backends.controls import SQLiteControlStore
        self.controls=SQLiteControlStore(self)
        from workbench_agent.persistence.backends.results import SQLiteResultStore
        self.results=SQLiteResultStore()
        from workbench_agent.persistence.usage import SQLiteUsageStore
        self.usage=SQLiteUsageStore()
        from workbench_agent.persistence.backends.tools import SQLiteToolStore
        self.tools=SQLiteToolStore()
        from workbench_agent.persistence.backends.runner_cleanup import SQLiteRunnerCleanupStore
        self.runner_cleanup=SQLiteRunnerCleanupStore(self.clock)
        from workbench_agent.persistence.backends.schedules import SQLiteScheduleStore
        self.schedules=SQLiteScheduleStore()
        from workbench_agent.persistence.backends.actions import SQLiteActionStore
        self.actions=SQLiteActionStore()
        from workbench_agent.persistence.backends.feedback import SQLiteFeedbackStore
        self.feedback=SQLiteFeedbackStore()
        from workbench_agent.persistence.backends.knowledge import SQLiteKnowledgeStore
        self.knowledge=SQLiteKnowledgeStore()
        from workbench_agent.persistence.backends.statistics import SQLiteStatisticsStore
        self.statistics=SQLiteStatisticsStore()
        from workbench_agent.persistence.backends.reviews import SQLiteReviewStore
        self.reviews=SQLiteReviewStore()

    def create_uow(self):
        return SQLiteUnitOfWork(self)

    def translate(self,exc):
        original = getattr(exc,"orig",exc)
        code = getattr(original,"sqlite_errorcode",None)
        base = code & 255 if code is not None else None
        if base in {sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED,sqlite3.SQLITE_INTERRUPT}:
            return StorageError("STORAGE_BUSY")
        if code in {sqlite3.SQLITE_CONSTRAINT_UNIQUE,sqlite3.SQLITE_CONSTRAINT_PRIMARYKEY}:
            return StorageError("UNIQUE_CONFLICT")
        if base in {sqlite3.SQLITE_FULL,sqlite3.SQLITE_IOERR,sqlite3.SQLITE_CORRUPT,sqlite3.SQLITE_NOTADB}:
            self._ready = False
        return StorageError("STORAGE_UNAVAILABLE")

    async def health(self):
        from workbench_agent.persistence.backends.sqlite_backup import quarantine_path
        if not self._ready or quarantine_path(self.path).exists():
            return False
        from workbench_agent.persistence.sqlite_diagnostics import open_readonly_connection, validate_required_schema, EXPECTED_PRAGMAS
        from workbench_agent.settings import SCHEMA_VERSION, CHECKPOINT_CODEC_VERSION
        from contextlib import closing
        deadline = time.monotonic()+self.operation_timeout
        def inspect():
            with closing(open_readonly_connection(self.path)) as db:
                db.set_progress_handler(lambda:int(time.monotonic()>=deadline),100)
                def budget():
                    remaining = deadline-time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError
                    db.execute(f"PRAGMA busy_timeout={max(0,min(100,int(remaining*1000)-150))}")
                # Check initial connection settings before reducing busy_timeout.
                for key,value in EXPECTED_PRAGMAS.items():
                    if db.execute(f"PRAGMA {key}").fetchone()[0] != value:
                        return False
                budget()
                rows = db.execute("SELECT schema_version,checkpoint_codec_version FROM dod_storage_metadata LIMIT 2").fetchall()
                if rows != [(SCHEMA_VERSION,CHECKPOINT_CODEC_VERSION)]:
                    return False
                return not validate_required_schema(db,budget)
        try:
            ready = await asyncio.to_thread(inspect)
            if not ready:
                self._ready = False
            return ready
        except sqlite3.Error as error:
            code = getattr(error,"sqlite_errorcode",0) & 255
            if code not in {sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED,sqlite3.SQLITE_INTERRUPT}:
                self._ready = False
            return False
        except TimeoutError:
            return False
        except OSError:
            self._ready = False
            return False

    async def close(self):
        await self.engine.dispose()

class SQLiteUnitOfWork:
    def __init__(self,backend):
        self.backend = backend
        self.connection = None
        self.closed = True
        self._active = False
        self._used = False
        self._failed = False

    def begin_read(self):
        return self._begin(False)

    def begin_write(self):
        return self._begin(True)

    @asynccontextmanager
    async def _begin(self,write):
        if self._used:
            raise RuntimeError("UoW cannot be reused")
        self._used = True
        self.deadline = time.monotonic() + self.backend.operation_timeout
        raw = None
        try:
            from workbench_agent.persistence.backends.sqlite_backup import quarantine_path
            if not self.backend._ready or write and quarantine_path(self.backend.path).exists():
                raise StorageError("STORAGE_UNAVAILABLE")
            self.connection = await self._connect()
            self.closed = False
            raw = (await self.connection.get_raw_connection()).driver_connection
            await raw.set_progress_handler(lambda:int(time.monotonic() >= self.deadline),100)
            await self._budget()
            if not write:
                await self.connection.exec_driver_sql("PRAGMA query_only=ON")
            await self._driver(lambda:self.connection.exec_driver_sql("BEGIN IMMEDIATE" if write else "BEGIN"))
            self._active = True
            # A failed restore acknowledgment can recreate quarantine while this
            # writer waits behind its BEGIN IMMEDIATE. Recheck after acquiring
            # that same SQLite write lock, before exposing any mutation surface.
            if write and quarantine_path(self.backend.path).exists():
                raise StorageError("STORAGE_UNAVAILABLE")
            await self._budget()
            yield self
            if self._active:
                await self.commit()
        except DBAPIError as exc:
            raise self.backend.translate(exc) from None
        finally:
            if self.connection is not None:
                try:
                    try:
                        # Cancellation may already have invalidated/closed the
                        # driver. Never reacquire it with an invalid transaction:
                        # SQLAlchemy would reconnect and mask CancelledError.
                        if raw is not None and not self.connection.invalidated and not self.connection.closed:
                            await raw.set_progress_handler(None,0)
                    finally:
                        # For invalidated connections this clears SQLAlchemy's
                        # transaction state without opening another connection.
                        await self.connection.rollback()
                finally:
                    try:
                        await self.connection.close()
                    finally:
                        self.closed = True
                        self._active = False

    async def _connect(self):
        # A new process may race another writer while inspecting WAL/schema.
        # Retry connection setup itself inside the same transaction deadline.
        while True:
            if time.monotonic() >= self.deadline:
                raise StorageError("STORAGE_BUSY")
            try:
                return await self.backend.engine.connect()
            except DBAPIError as exc:
                code = getattr(exc.orig,"sqlite_errorcode",0) & 255
                if code not in {sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED} or time.monotonic()+0.15 >= self.deadline:
                    raise
                await asyncio.sleep(0.01)

    async def _budget(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            self._failed = True
            raise StorageError("STORAGE_BUSY")
        # Reserve cleanup time; each call receives only the remaining total budget.
        await self.connection.exec_driver_sql(f"PRAGMA busy_timeout={max(0,min(100,int(remaining*1000)-150))}")

    async def _driver(self,operation):
        # SQLite busy handlers can oversleep their configured timeout. Use short
        # driver waits against ONE deadline, leaving headroom for rollback/close.
        while True:
            await self._budget()
            try:
                return await operation()
            except DBAPIError as exc:
                code = getattr(exc.orig,"sqlite_errorcode",0) & 255
                if code not in {sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED} or time.monotonic()+0.15 >= self.deadline:
                    raise
                await asyncio.sleep(0.01)

    async def execute(self,statement,parameters=None):
        if self.closed or not self._active or self._failed:
            raise RuntimeError("transaction is closed or failed")
        try:
            return await self._driver(lambda:self.connection.execute(statement,parameters or {}))
        except DBAPIError as exc:
            self._failed = True
            raise self.backend.translate(exc) from None

    async def commit(self):
        if not self._active or self._failed:
            raise RuntimeError("transaction is closed or failed")
        try:
            await self._driver(self.connection.commit)
            self._active = False
        except DBAPIError as exc:
            self._failed = True
            raise self.backend.translate(exc) from None

    async def rollback(self):
        if self.connection is None or self.closed:
            raise RuntimeError("transaction is closed")
        await self.connection.rollback()
        self._active = False

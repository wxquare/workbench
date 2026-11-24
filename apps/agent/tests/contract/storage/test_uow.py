import sqlite3
import time
import pytest
from sqlalchemy import text


@pytest.fixture
async def backend(tmp_path):
    from workbench_agent.cli.db import migrate
    from workbench_agent.persistence.backends.sqlite import SQLiteBackend

    path = tmp_path / 'storage.db'
    migrate(f'sqlite+aiosqlite:///{path}')
    backend = SQLiteBackend(f'sqlite+aiosqlite:///{path}')
    yield backend
    await backend.close()


async def test_failed_transaction_rolls_back_all_rows(backend):
    from support.storage import SeedTeams

    with pytest.raises(RuntimeError, match='crash'):
        async with backend.create_uow().begin_write() as uow:
            await SeedTeams(uow).add('team-a')
            raise RuntimeError('crash')
    async with backend.create_uow().begin_read() as uow:
        assert await SeedTeams(uow).count() == 0


async def test_commit_rollback_and_closed_connection(backend):
    from support.storage import SeedTeams

    async with backend.create_uow().begin_write() as uow:
        await SeedTeams(uow).add('committed')
        await uow.commit()
    assert uow.closed
    async with backend.create_uow().begin_write() as uow:
        await SeedTeams(uow).add('rolled-back')
        await uow.rollback()
    async with backend.create_uow().begin_read() as uow:
        assert await SeedTeams(uow).count() == 1
    with pytest.raises(RuntimeError):
        await uow.execute(text('SELECT 1'))


async def test_clock_and_connection_settings(backend):
    async with backend.create_uow().begin_read() as uow:
        before = int(time.time() * 1000)
        now = await backend.clock.now_ms(uow)
        assert before - 2 <= now <= int(time.time() * 1000) + 2
        for name, expected in [
            ('foreign_keys', 1),
            ('journal_mode', 'wal'),
            ('synchronous', 2),
            ('wal_autocheckpoint', 1000),
        ]:
            assert (await uow.execute(text(f'PRAGMA {name}'))).scalar_one() == expected


async def test_unique_conflict_does_not_leave_partial_rows(backend):
    from workbench_agent.persistence.errors import StorageError
    from support.storage import SeedTeams

    async with backend.create_uow().begin_write() as uow:
        await SeedTeams(uow).add('existing')
    with pytest.raises(StorageError) as error:
        async with backend.create_uow().begin_write() as uow:
            await SeedTeams(uow).add('partial')
            await SeedTeams(uow).add('existing')
    assert error.value.code == 'UNIQUE_CONFLICT'
    async with backend.create_uow().begin_read() as uow:
        assert await SeedTeams(uow).count() == 1


async def test_cross_team_agent_reference_is_rejected(backend):
    from workbench_agent.persistence.errors import StorageError

    async with backend.create_uow().begin_write() as uow:
        await uow.execute(
            text(
                "INSERT INTO teams(team_id,name,status) VALUES ('a','a','active'),('b','b','active')"
            )
        )
        await uow.execute(
            text("INSERT INTO agents(team_id,agent_id,name) VALUES ('a','w','agent')")
        )
    with pytest.raises(StorageError):
        async with backend.create_uow().begin_write() as uow:
            await uow.execute(
                text("INSERT INTO threads(team_id,thread_id,agent_id) VALUES ('b','t','w')")
            )


async def test_read_transaction_cannot_write(backend):
    from workbench_agent.persistence.errors import StorageError

    with pytest.raises(StorageError):
        async with backend.create_uow().begin_read() as uow:
            await uow.execute(text('DELETE FROM teams'))


def test_constructor_never_creates_or_migrates_database(tmp_path):
    from workbench_agent.persistence.backends.sqlite import SQLiteBackend

    path = tmp_path / 'absent.db'
    SQLiteBackend(f'sqlite+aiosqlite:///{path}')
    assert not path.exists()


def test_migration_is_repeatable_and_enforces_circular_references(tmp_path):
    from workbench_agent.cli.db import migrate

    path = tmp_path / 'db'
    url = f'sqlite+aiosqlite:///{path}'
    migrate(url)
    migrate(url)
    with sqlite3.connect(path) as db:
        db.execute('PRAGMA foreign_keys=ON')
        assert db.execute('SELECT * FROM dod_storage_metadata').fetchall() == [
            ('dod-storage-agent-v1', 'dod-checkpoint-v1')
        ]
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        db.execute("INSERT INTO teams(team_id,name,status) VALUES ('a','a','active')")
        db.execute("INSERT INTO agents(team_id,agent_id,name) VALUES ('a','w','w')")
        db.execute("INSERT INTO threads(team_id,thread_id,agent_id) VALUES ('a','t','w')")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE threads SET active_run_id='missing' WHERE thread_id='t'")


async def test_failed_integrity_readiness_stops_new_writes(backend):
    from workbench_agent.persistence.errors import StorageError

    assert await backend.health()
    with sqlite3.connect(backend.path) as db:
        db.execute('DROP INDEX ix_claimable_jobs')
    assert not await backend.health()
    with pytest.raises(StorageError) as error:
        async with backend.create_uow().begin_write() as uow:
            await uow.execute(text('SELECT 1'))
    assert error.value.code == 'STORAGE_UNAVAILABLE'


async def test_disk_full_rolls_back_and_disables_backend(backend):
    from workbench_agent.persistence.errors import StorageError
    from support.storage import SeedTeams

    with pytest.raises(StorageError) as error:
        async with backend.create_uow().begin_write() as uow:
            pages = (await uow.execute(text('PRAGMA page_count'))).scalar_one()
            await uow.execute(text(f'PRAGMA max_page_count={pages}'))
            await SeedTeams(uow).add('partial')
            await uow.execute(
                text("INSERT INTO teams(team_id,name,status) VALUES ('large',:name,'active')"),
                {'name': 'x' * 2000000},
            )
    assert error.value.code == 'STORAGE_UNAVAILABLE'
    assert not await backend.health()
    with sqlite3.connect(backend.path) as db:
        assert db.execute('SELECT count(*) FROM teams').fetchone()[0] == 0
    with pytest.raises(StorageError):
        async with backend.create_uow().begin_write():
            pass


async def test_query_deadline_rolls_back_earlier_write(backend):
    from workbench_agent.persistence.backends.sqlite import SQLiteBackend
    from workbench_agent.persistence.errors import StorageError
    from support.storage import SeedTeams

    short = SQLiteBackend(f'sqlite+aiosqlite:///{backend.path}', operation_timeout=0.2)
    started = time.monotonic()
    try:
        with pytest.raises(StorageError) as error:
            async with short.create_uow().begin_write() as uow:
                await SeedTeams(uow).add('partial')
                await uow.execute(
                    text(
                        'WITH RECURSIVE nums(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM nums WHERE n<100000000) SELECT sum(n) FROM nums'
                    )
                )
        assert error.value.code == 'STORAGE_BUSY'
        assert time.monotonic() - started < 0.5
        assert uow.closed
    finally:
        await short.close()
    async with backend.create_uow().begin_read() as uow:
        assert await SeedTeams(uow).count() == 0


async def test_missing_database_operation_never_creates_file(tmp_path):
    from workbench_agent.persistence.backends.sqlite import SQLiteBackend
    from workbench_agent.persistence.errors import StorageError

    path = tmp_path / 'absent.db'
    absent = SQLiteBackend(f'sqlite+aiosqlite:///{path}')
    try:
        with pytest.raises(StorageError) as error:
            async with absent.create_uow().begin_write():
                pass
        assert error.value.code == 'STORAGE_UNAVAILABLE'
        assert not path.exists()
    finally:
        await absent.close()


def test_postgres_ddl_does_not_collide_on_unique_constraint_names():
    # PostgreSQL creates a schema-wide named index for each UNIQUE constraint.
    import re
    from sqlalchemy.schema import CreateTable
    from sqlalchemy.dialects.postgresql import dialect
    from workbench_agent.persistence.schema import metadata

    names = []
    for table in metadata.tables.values():
        ddl = str(CreateTable(table).compile(dialect=dialect()))
        names.extend(re.findall(r'CONSTRAINT (\w+) UNIQUE', ddl))
    assert len(names) == len(set(names)), 'portable migration emits colliding unique index names'


async def test_cancelled_sql_preserves_cancellation_and_rolls_back(backend):
    import asyncio
    import threading
    from workbench_agent.persistence.backends.sqlite import SQLiteBackend
    from support.storage import SeedTeams

    short = SQLiteBackend(f'sqlite+aiosqlite:///{backend.path}', operation_timeout=0.4)
    uow = short.create_uow()
    executing = threading.Event()

    def signal_execution():
        executing.set()
        return 1

    async def write_then_query():
        async with uow.begin_write():
            await SeedTeams(uow).add('cancelled')
            raw = (await uow.connection.get_raw_connection()).driver_connection
            await raw.create_function('signal_execution', 0, signal_execution)
            await uow.execute(
                text(
                    'WITH RECURSIVE nums(n) AS (SELECT signal_execution() UNION ALL SELECT n+1 FROM nums WHERE n<100000000) SELECT sum(n) FROM nums'
                )
            )

    task = asyncio.create_task(write_then_query())
    try:
        assert await asyncio.to_thread(executing.wait, 2), 'real SQLite query never started'
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert uow.closed
        # A fresh write transaction must acquire the lock and see no partial row.
        async with asyncio.timeout(1):
            async with backend.create_uow().begin_write() as next_uow:
                assert await SeedTeams(next_uow).count() == 0
                await SeedTeams(next_uow).add('after-cancellation')
            async with backend.create_uow().begin_read() as next_uow:
                assert await SeedTeams(next_uow).count() == 1
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await short.close()
